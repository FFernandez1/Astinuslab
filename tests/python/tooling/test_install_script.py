import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INSTALL = ROOT / "ops/deploy/install.sh"
UNINSTALL = ROOT / "ops/deploy/uninstall.sh"
PRIVILEGED_ROOTS = ROOT / "ops/deploy/configure-privileged-roots.sh"
ACL_SNAPSHOT = ROOT / "ops/deploy/acl_snapshot.py"
SSH_KEY = ROOT / "ops/deploy/ssh_key.py"
PERMISSIONS = ROOT / "ops/deploy/permissions.py"
pytestmark = [pytest.mark.unit, pytest.mark.host]

EXPECTED_INSTALL_PHASES = [
    "[1/9] Validate host and source",
    "[2/9] Resolve mode, identities, and trusted roots",
    "[3/9] Install host prerequisites",
    "[4/9] Stage release and Python environment",
    "[5/9] Prepare configuration, storage, and databases",
    "[6/9] Configure authentication and guest SSH",
    "[7/9] Install privileged host integration",
    "[8/9] Publish services and verify readiness",
    "[9/9] Present credentials and next actions",
]

# Every INSTALL_DIR ops/deploy/install.sh refuses to create. uninstall.sh runs as
# root and `rm -rf`s this path, so it has to refuse exactly the same set.
UNSAFE_INSTALL_DIRS = (
    "relative/path",
    "./relative",
    "/",
    "/etc",
    "/usr",
    "/var",
    "/home",
    "/bin",
    "/sbin",
    "/lib",
    "/sys",
    "/dev",
    "/proc",
    "/root",
    "/tmp",
    "/opt",
)


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

    ``run_root_mapped_bash`` cannot stage an *unprivileged* owner: inside a user
    namespace ``chown`` is refused with EPERM because the kernel checks CAP_CHOWN
    against the namespace owning the filesystem's superblock -- the initial one
    for anything under /tmp. A discarded EPERM there leaves the file root-owned,
    the custody guard correctly does nothing, and the test passes over a
    condition it never built. fakeroot intercepts the stat family instead, so the
    parent still reads as root-owned (which the guard requires before it will
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
    """Stage one flavour of unsafe custody, failing loudly if it does not take."""
    if unsafe_custody == "writable":
        template.chmod(0o666)
        return ":"
    return f"chown 1:1 {shlex.quote(str(template))} || exit 92"


@pytest.mark.parametrize(
    "operation",
    ["config", "databases", "install-root", "helper-root", "helper-payload", "helper-match", "sudoers-root"],
)
def test_deploy_python_imports_leave_source_tree_immutable(tmp_path, operation):
    source = tmp_path / "source"
    deploy = source / "ops/deploy"
    host = source / "ops/host"
    deploy.mkdir(parents=True)
    host.mkdir()
    for name in ("managed_config.py", "runtime_environment.py", "shared_databases.py"):
        shutil.copyfile(ROOT / "ops/deploy" / name, deploy / name)
    shutil.copyfile(ROOT / "ops/host/acl_policy.py", host / "acl_policy.py")
    (tmp_path / "install/storage").mkdir(parents=True)
    (tmp_path / "install/storage").chmod(0o750)
    # The policy rightly rejects /tmp. Only this new mount namespace sees the
    # disposable fixture at an admissible deployment path.
    install = Path("/srv/install")
    storage = install / "storage"
    default = tmp_path / "default.toml"
    default.write_text("value = 1\n")
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": "astinuslab-acl-policy/v1",
                "allowed_callers": ["root"],
                "allowed_roots": [str(storage)],
                "storage_groups": [],
                "evidence_read_groups": [],
            }
        )
    )
    policy.chmod(0o644)

    def snapshot():
        return {
            str(path.relative_to(source)): (
                path.stat().st_mode,
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
            )
            for path in source.rglob("*")
        }

    before = snapshot()
    commands = {
        "config": f"install_config_if_missing {shlex.quote(str(default))} {shlex.quote(str(install / 'config.toml'))}",
        "databases": "split_database_layout pending",
        "install-root": "validate_install_storage_root",
        "helper-root": "validate_root_with_authority",
        "helper-payload": "policy_payload",
        "helper-match": "json_matches",
        "sudoers-root": "validate_storage_root_authority",
    }
    entry = PRIVILEGED_ROOTS if operation.startswith("helper-") else INSTALL
    if operation == "sudoers-root":
        entry = ROOT / "ops/host/install_sudoers_astinuslab.sh"
    script = f"""
        mount --bind {shlex.quote(str(tmp_path))} /srv || exit 92
        source {shlex.quote(str(entry))}
        unset PYTHONDONTWRITEBYTECODE PYTHONPYCACHEPREFIX
        SCRIPT_DIR={shlex.quote(str(deploy))}
        REPO_DIR={shlex.quote(str(source))}
        SOURCE_DIR={shlex.quote(str(source))}
        HELPER_SOURCE_DIR={shlex.quote(str(host))}
        INSTALL_DIR={shlex.quote(str(install))}
        STORAGE_ROOT={shlex.quote(str(storage))}
        POLICY_FILE={shlex.quote(str(policy))}
        SERVICE_USER=root
        assert_install_source_authority() {{ :; }}
        {commands[operation]}
        """
    result = subprocess.run(
        ["unshare", "--map-root-user", "--map-auto", "--mount", "bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert snapshot() == before, "Deploy imports must not add bytecode or modify supplied source"


def test_install_script_has_valid_shell_syntax():
    result = subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_install_script_source_is_inert_and_defines_functions():
    result = run_bash(f"source {INSTALL}; type -t main; type -t parse_args; type -t install_runtime_config_defaults")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["function", "function", "function"]

    assert not (ROOT / "deploy" / "install.sh").exists(), "the expired root wrapper must stay removed"


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (
            "",
            [
                "Action:          clean install",
                "Runtime users:   operator",
                "Authentication:  generate admin token",
            ],
        ),
        ('mkdir -p "$INSTALL_DIR"; ln -s /release "$INSTALL_DIR/current"', ["Action:          update"]),
        (
            "SPLIT_USERS=true; API_USER=astinuslab-api; RUNNER_USER=astinuslab-runner; "
            "ANALYSIS_USER=astinuslab-analysis",
            [
                "Action:          split-user migration",
                "Runtime users:   astinuslab-api, astinuslab-runner, astinuslab-analysis",
            ],
        ),
        (
            "SPLIT_USERS=true; API_USER=astinuslab-api; RUNNER_USER=astinuslab-runner; "
            "ANALYSIS_USER=astinuslab-analysis; "
            'RUNTIME_IDENTITIES_FILE="$INSTALL_DIR/runtime-identities"; '
            'mkdir -p "$INSTALL_DIR"; touch "$RUNTIME_IDENTITIES_FILE"',
            ["Action:          split-user update"],
        ),
        ("ALLOW_UNAUTHENTICATED_LOCAL=true", ["Authentication:  disabled by explicit flag"]),
        ("START_SERVICES=false", ["Services:        install only"]),
    ],
)
def test_install_plan_reports_resolved_choices(tmp_path, setup, expected):
    install = tmp_path / "install"
    result = run_bash(
        f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        STORAGE_ROOT="$INSTALL_DIR/storage"
        SERVICE_USER=operator
        RUNNER_USER=operator
        {setup}
        print_install_plan
        """
    )

    assert result.returncode == 0, result.stdout + result.stderr
    for line in expected:
        assert line in result.stdout
    assert "VM images:       downloaded after installation" in result.stdout
    assert "Volatility 3:    operator-supplied; guided separately" in result.stdout


def test_install_dir_rejects_paths_that_cannot_be_safely_rendered():
    result = run_bash(f"source {INSTALL}; validate_install_dir", env={"INSTALL_DIR": "/opt/astinuslab unsafe"})

    assert result.returncode != 0
    assert "unsupported characters" in result.stderr


def test_full_main_defers_python_install_root_state_check_until_prerequisites(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    events = tmp_path / "events"
    result = run_bash(f"""
        source {INSTALL}
        python_ready=false
        command() {{
          if [[ "$1" == -v && "$2" == python3 ]]; then $python_ready; return; fi
          builtin command "$@"
        }}
        python3() {{
          $python_ready || {{ echo premature-python >> {shlex.quote(str(events))}; return 127; }}
          command python3 "$@"
        }}
        resolve_storage_root() {{ :; }}
        validate_install_storage_root() {{ :; }}
        validate_existing_install_dir_authority() {{ :; }}
        verify_storage_root_matches_config() {{ :; }}
        require_root() {{ :; }}
        refuse_foreign_template_instances() {{ :; }}
        check_prerequisites() {{ echo prerequisites >> {shlex.quote(str(events))}; python_ready=true; }}
        ensure_service_user() {{ :; }}
        upgrade_single_identity_install() {{ :; }}
        print_success() {{ :; }}
        main --install-dir {shlex.quote(str(install))} --service-user operator -y
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == ["prerequisites"]

    text = INSTALL.read_text(encoding="utf-8")
    main = text[text.index("main() {") :]
    assert main.index("check_prerequisites") < main.index(
        'validate_existing_install_dir_state || die "Unsafe existing install root"',
        main.index("check_prerequisites"),
    )


def test_create_venv_propagates_the_python_venv_failure(tmp_path):
    result = run_bash(
        f"""
        source {INSTALL}
        RELEASE_DIR={shlex.quote(str(tmp_path / "release"))}
        python3() {{ return 1; }}
        if create_venv; then exit 99; fi
        """
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Venv created" not in result.stdout


def test_debian_prerequisites_install_venv_support_when_python_lacks_ensurepip(tmp_path):
    apt_log = tmp_path / "apt.log"
    result = run_bash(
        f"""
        source {INSTALL}
        venv_ready=false
        detect_os_family() {{ echo debian; }}
        command() {{
          if [[ "$1" == -v ]]; then return 0; fi
          builtin command "$@"
        }}
        python3() {{
          if [[ "$*" == *ensurepip* ]]; then $venv_ready; return; fi
          return 0
        }}
        apt-get() {{
          echo "$*" >> {shlex.quote(str(apt_log))}
          [[ "$1" != install ]] || venv_ready=true
        }}
        virsh() {{ return 0; }}
        check_prerequisites
        """
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "python3-venv" in apt_log.read_text(encoding="utf-8")


def test_debian_prerequisites_install_native_build_toolchain_when_cc_is_missing(tmp_path):
    """libvirt-python is locked as source and needs both a compiler and Python
    headers; a pristine Ubuntu cloud image intentionally ships neither."""
    apt_log = tmp_path / "apt.log"
    result = run_bash(
        f"""
        source {INSTALL}
        detect_os_family() {{ echo debian; }}
        command() {{
          if [[ "$1" == -v && "$2" == cc ]]; then return 1; fi
          if [[ "$1" == -v ]]; then return 0; fi
          builtin command "$@"
        }}
        python3() {{ return 0; }}
        apt-get() {{ echo "$*" >> {shlex.quote(str(apt_log))}; }}
        virsh() {{ return 0; }}
        check_prerequisites
        """
    )

    assert result.returncode == 0, result.stdout + result.stderr
    packages = apt_log.read_text(encoding="utf-8")
    assert "build-essential" in packages
    assert "python3-dev" in packages


def test_debian_prerequisites_install_python_headers_when_python_is_initially_absent(tmp_path):
    apt_log = tmp_path / "apt.log"
    result = run_bash(
        f"""
        source {INSTALL}
        python_ready=false
        detect_os_family() {{ echo debian; }}
        command() {{
          if [[ "$1" == -v && "$2" == python3 ]]; then $python_ready; return; fi
          if [[ "$1" == -v ]]; then return 0; fi
          builtin command "$@"
        }}
        python3() {{ $python_ready; }}
        pkg-config() {{ return 0; }}
        apt-get() {{
          echo "$*" >> {shlex.quote(str(apt_log))}
          [[ "$1" != install ]] || python_ready=true
        }}
        virsh() {{ return 0; }}
        check_prerequisites
        """
    )

    assert result.returncode == 0, result.stdout + result.stderr
    packages = apt_log.read_text(encoding="utf-8")
    assert "python3-venv" in packages
    assert "python3-dev" in packages


def test_debian_prerequisites_probe_python_headers_independently(tmp_path):
    apt_log = tmp_path / "apt.log"
    result = run_bash(
        f"""
        source {INSTALL}
        headers_ready=false
        detect_os_family() {{ echo debian; }}
        command() {{
          if [[ "$1" == -v ]]; then return 0; fi
          builtin command "$@"
        }}
        python3() {{
          if [[ "$*" == *Python.h* ]]; then $headers_ready; return; fi
          return 0
        }}
        pkg-config() {{ return 0; }}
        apt-get() {{
          echo "$*" >> {shlex.quote(str(apt_log))}
          [[ "$1" != install ]] || headers_ready=true
        }}
        virsh() {{ return 0; }}
        check_prerequisites
        """
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "python3-dev" in apt_log.read_text(encoding="utf-8")


def test_debian_prerequisites_probe_libvirt_headers_independently(tmp_path):
    apt_log = tmp_path / "apt.log"
    result = run_bash(
        f"""
        source {INSTALL}
        libvirt_headers_ready=false
        detect_os_family() {{ echo debian; }}
        command() {{
          if [[ "$1" == -v ]]; then return 0; fi
          builtin command "$@"
        }}
        python3() {{ return 0; }}
        pkg-config() {{
          if [[ "$*" == "--exists libvirt" ]]; then $libvirt_headers_ready; return; fi
          return 0
        }}
        apt-get() {{
          echo "$*" >> {shlex.quote(str(apt_log))}
          [[ "$1" != install ]] || libvirt_headers_ready=true
        }}
        virsh() {{ return 0; }}
        check_prerequisites
        """
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "libvirt-dev" in apt_log.read_text(encoding="utf-8")


def test_install_script_has_upgrade_permission_reconciliation():
    text = INSTALL.read_text(encoding="utf-8")
    assert "reconcile_install_permissions()" in text
    assert "setfacl -bR" in text
    assert "chown -R root:root" in text
    assert 'find "$path" -type d -exec chmod g-s' in text
    assert 'find "$INSTALL_DIR" -type d -exec chmod g+s {} +' not in text
    assert 'chown -R "$SERVICE_USER:astinuslab" "$INSTALL_DIR/.venv"' not in text


def test_check_reports_symlinked_existing_root_without_any_mutation(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "backend"
    sentinel.write_text("operator-owned\n", encoding="utf-8")
    install_link = tmp_path / "install"
    install_link.symlink_to(outside, target_is_directory=True)

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install_link))}
        migration_snapshot() {{ echo FORBIDDEN-snapshot; }}
        stage_release() {{ echo FORBIDDEN-stage; }}
        main --check
    """)

    assert result.returncode != 0
    assert "FORBIDDEN" not in result.stdout
    assert "Preflight result: NOT READY" in result.stdout
    assert sentinel.read_text(encoding="utf-8") == "operator-owned\n"


def test_install_refuses_user_owned_existing_root_before_staging(tmp_path):
    install = tmp_path / "install"
    install.mkdir(mode=0o755)
    sentinel = install / "current"
    sentinel.write_text("operator-owned\n", encoding="utf-8")

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        stage_release() {{ echo FORBIDDEN-stage; }}
        validate_existing_install_dir_authority
        stage_release
    """)

    assert result.returncode != 0
    assert "FORBIDDEN" not in result.stdout
    assert sentinel.read_text(encoding="utf-8") == "operator-owned\n"


def test_install_refuses_an_absent_root_below_a_user_writable_parent(tmp_path):
    install = tmp_path / "missing" / "install"

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        validate_existing_install_dir_authority
        echo FORBIDDEN-mutation
    """)

    assert result.returncode != 0
    assert "FORBIDDEN" not in result.stdout
    assert not install.exists()
    assert "every existing ancestor" in result.stderr


def test_install_accepts_an_absent_root_below_trusted_opt():
    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR=/opt/astinuslab-parent-authority-test-missing
        validate_existing_install_dir_authority
    """)

    assert result.returncode == 0, result.stdout + result.stderr


def test_uninstall_validates_parent_authority_before_service_mutation():
    text = UNINSTALL.read_text(encoding="utf-8")

    assert "validate_install_dir_authority" in text
    assert text.index('validate_install_dir_authority || die "Unsafe install-root parent authority"') < text.index(
        "SERVICE_UNITS=("
    )


def test_privileged_root_config_rejects_operator_owned_source_before_import(tmp_path):
    install = tmp_path / "legacy-install"
    release = install / "releases" / "malicious"
    (release / "ops/systemd").mkdir(parents=True)
    (release / "ops/host").mkdir(parents=True)
    (release / "ops" / "deploy").mkdir(parents=True)
    (install / "current").symlink_to("releases/malicious")
    marker = tmp_path / "executed"
    (release / "ops/deploy/install.sh").write_text(f"touch {marker}\n", encoding="utf-8")
    for name in ("astinuslab-api", "astinuslab-runner", "astinuslab-analysis"):
        (release / f"ops/systemd/{name}.service").write_text("[Service]\n", encoding="utf-8")
    (release / "ops/host/acl_policy.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")

    result = run_bash(f"""
        source {ROOT / "ops/deploy/configure-privileged-roots.sh"}
        INSTALL_DIR={shlex.quote(str(install))}
        bind_install_source_authority
    """)

    assert result.returncode != 0
    assert not marker.exists()
    assert "root:root" in result.stderr


def test_privileged_root_config_binds_source_before_import_or_identity_detection():
    text = (ROOT / "ops/deploy/configure-privileged-roots.sh").read_text(encoding="utf-8")
    main = text[text.index("main() {") :]

    assert main.index("bind_install_source_authority") < main.index("detect_runtime_identities")
    assert "assert_install_source_authority || return 1" in text


def test_split_source_reconciliation_defers_every_mutable_tree_until_quiesce():
    text = INSTALL.read_text(encoding="utf-8")
    reconcile = text[text.index("reconcile_install_permissions()") : text.index("secure_admin_config()")]
    split_upgrade = text[
        text.index("upgrade_existing_split_install()") : text.index("bootstrap_split_candidate_if_missing()")
    ]

    assert 'immutable_only="${1:-false}"' in reconcile
    assert 'if $SPLIT_USERS || [[ "$immutable_only" == true ]]; then return 0; fi' in reconcile
    assert "install_split_upgrade_source" in split_upgrade
    assert "install_source ||" not in split_upgrade
    assert "prepare_storage ||" not in split_upgrade
    assert split_upgrade.index("quiesce_services") < split_upgrade.index("prepare_split_upgrade_mutable_state")


def test_single_upgrade_defers_mutable_state_and_snapshot_until_quiesce():
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("upgrade_single_identity_install()") : text.index("upgrade_existing_split_install()")]

    assert "install_source true" in body
    assert body.index("quiesce_services") < body.index("migration_snapshot_mutable_state")
    assert body.index("migration_snapshot_mutable_state") < body.index("prepare_single_upgrade_mutable_state")
    assert body.index("prepare_single_upgrade_mutable_state") < body.index("prepare_storage")


def test_upgrade_flows_never_rebuild_the_staged_venv_before_quiesce():
    text = INSTALL.read_text(encoding="utf-8")
    single = text[text.index("upgrade_single_identity_install()") : text.index("upgrade_existing_split_install()")]
    split = text[text.index("upgrade_existing_split_install()") : text.index("bootstrap_split_candidate_if_missing()")]
    bootstrap = text[text.index("bootstrap_split_candidate_if_missing()") : text.index("validate_split_candidate()")]

    assert "create_venv" not in single
    assert "create_venv" not in split
    assert "create_venv" not in bootstrap


def test_stage_release_refuses_to_rebuild_a_missing_live_venv(tmp_path):
    install = tmp_path / "install"
    release = install / "releases/same"
    release.mkdir(parents=True)
    (install / "current").symlink_to(release)
    events = tmp_path / "events"

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        EVENTS={shlex.quote(str(events))}
        sync_source_tree() {{ :; }}
        release_id_for() {{ printf 'same\\n'; }}
        create_venv() {{ echo rebuilt-live-venv >> "$EVENTS"; }}
        if stage_release; then exit 99; fi
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not events.exists()
    assert "current release has no usable virtualenv" in (result.stdout + result.stderr)


def test_legacy_source_cleanup_occurs_only_after_upgrade_commit():
    text = INSTALL.read_text(encoding="utf-8")
    single_prepare = text[
        text.index("prepare_single_upgrade_mutable_state()") : text.index("install_split_upgrade_source()")
    ]
    split_prepare = text[text.index("prepare_split_upgrade_mutable_state()") : text.index("create_venv()")]
    single = text[text.index("upgrade_single_identity_install()") : text.index("upgrade_existing_split_install()")]
    split = text[text.index("upgrade_existing_split_install()") : text.index("bootstrap_split_candidate_if_missing()")]
    migration = text[text.index("migrate_to_split_users()") : text.index("rollback_existing_split_upgrade()")]

    assert "remove_legacy_flat_source" not in single_prepare
    assert "remove_legacy_vol3_bin_files" not in single_prepare
    assert "remove_legacy_flat_source" not in split_prepare
    assert "remove_legacy_vol3_bin_files" not in split_prepare
    assert single.index("verify_release_readiness") < single.index("migration_discard_snapshot")
    assert single.index("migration_discard_snapshot") < single.index("cleanup_superseded_legacy_install")
    assert split.index("verify_split_services") < split.index("migration_commit")
    assert split.index("migration_commit") < split.index("cleanup_superseded_legacy_install")
    assert migration.index("migration_commit") < migration.index("cleanup_superseded_legacy_install")


def test_late_single_upgrade_failure_retains_flat_source_for_rollback(tmp_path):
    install = tmp_path / "install"
    release = install / "releases/new"
    (release / "backend").mkdir(parents=True)
    (install / "backend").mkdir()
    (install / "backend/legacy.py").write_text("legacy runtime\n", encoding="utf-8")
    legacy_bin = _seed_legacy_vol3_bin(install)
    events = tmp_path / "events"

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        RELEASE_DIR={shlex.quote(str(release))}
        EVENTS={shlex.quote(str(events))}
        migration_snapshot() {{ :; }}
        install_source() {{ :; }}
        create_venv() {{ :; }}
        quiesce_services() {{ :; }}
        migration_snapshot_mutable_state() {{ :; }}
        reconcile_install_permissions() {{ :; }}
        install_runtime_config_defaults() {{ :; }}
        configure_admin_auth() {{ :; }}
        prepare_storage() {{ :; }}
        backup_database_before_migration() {{ :; }}
        apply_db_migrations() {{ :; }}
        handle_ssh_key() {{ :; }}
        resolve_storage_root() {{ :; }}
        bind_storage_root_identity() {{ :; }}
        assert_storage_root_identity() {{ :; }}
        install_sudoers_rule() {{ :; }}
        configure_mac() {{ :; }}
        install_systemd_units() {{ :; }}
        switch_release_and_restart() {{ :; }}
        verify_release_readiness() {{ return 1; }}
        rollback_existing_single_upgrade() {{ echo rollback >> "$EVENTS"; }}
        migration_discard_snapshot() {{ echo discarded >> "$EVENTS"; }}
        if upgrade_single_identity_install; then exit 99; fi
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == ["rollback"]
    assert (install / "backend/legacy.py").read_text(encoding="utf-8") == "legacy runtime\n"
    for name in LEGACY_VOL3_BIN_FILES:
        assert (legacy_bin / name).is_file()


def test_successful_single_upgrade_purges_legacy_tree_after_commit(tmp_path):
    install = tmp_path / "install"
    release = install / "releases/new"
    (release / "backend").mkdir(parents=True)
    (install / "backend").mkdir()
    (install / "backend/legacy.py").write_text("legacy runtime\n", encoding="utf-8")
    legacy_bin = _seed_legacy_vol3_bin(install)
    events = tmp_path / "events"

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        RELEASE_DIR={shlex.quote(str(release))}
        EVENTS={shlex.quote(str(events))}
        migration_snapshot() {{ :; }}
        install_source() {{ :; }}
        create_venv() {{ :; }}
        quiesce_services() {{ :; }}
        migration_snapshot_mutable_state() {{ :; }}
        reconcile_install_permissions() {{ :; }}
        install_runtime_config_defaults() {{ :; }}
        configure_admin_auth() {{ :; }}
        prepare_storage() {{ :; }}
        backup_database_before_migration() {{ :; }}
        apply_db_migrations() {{ :; }}
        handle_ssh_key() {{ :; }}
        resolve_storage_root() {{ :; }}
        bind_storage_root_identity() {{ :; }}
        assert_storage_root_identity() {{ :; }}
        install_sudoers_rule() {{ :; }}
        configure_mac() {{ :; }}
        install_systemd_units() {{ :; }}
        switch_release_and_restart() {{ :; }}
        verify_release_readiness() {{ :; }}
        rollback_existing_single_upgrade() {{ exit 98; }}
        migration_discard_snapshot() {{ echo committed >> "$EVENTS"; }}
        upgrade_single_identity_install
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == ["committed"]
    assert not (install / "backend").exists()
    for name in LEGACY_VOL3_BIN_FILES:
        assert not (legacy_bin / name).exists()


def test_split_migration_changes_storage_custody_only_after_old_services_stop():
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("migrate_to_split_users()") : text.index("rollback_existing_split_upgrade()")]

    assert body.index("stop_old_services") < body.index("migration_snapshot_mutable_state")
    assert body.index("migration_snapshot_mutable_state") < body.index("ensure_split_storage_root")
    assert body.index("ensure_split_storage_root") < body.index("configure_split_permissions")

    main = text[text.index("main() {") :]
    assert "migration_snapshot false" in main[: main.index("ensure_split_users")]


def test_managed_config_snapshot_restores_bytes_and_candidate_created_absence(tmp_path):
    install = tmp_path / "install"
    snapshot = tmp_path / "snapshot"
    (install / "config").mkdir(parents=True)
    snapshot.mkdir()
    config = install / "astinuslab.toml"
    env_file = install / "config/astinuslab.env"
    keys = install / "storage/ssh_keys"
    keys.mkdir(parents=True)
    config.write_bytes(b"[auth]\nadmin_token_hash = 'prior'\n")
    config.chmod(0o640)
    prior_inode = config.stat().st_ino

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        MIGRATION_SNAPSHOT={shlex.quote(str(snapshot))}
        snapshot_managed_config_state
        printf '%s\n' candidate > {shlex.quote(str(config))}
        printf '%s\n' candidate-env > {shlex.quote(str(env_file))}
        printf '%s\n' astinuslab-default > {shlex.quote(str(keys / ".active"))}
        printf '%s\n' candidate-private > {shlex.quote(str(keys / "astinuslab-default"))}
        printf '%s\n' candidate-public > {shlex.quote(str(keys / "astinuslab-default.pub"))}
        restore_managed_config_state
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_bytes() == b"[auth]\nadmin_token_hash = 'prior'\n"
    assert config.stat().st_ino == prior_inode, "rollback must preserve ACL snapshot custody"
    assert config.stat().st_mode & 0o777 == 0o640
    assert not env_file.exists()
    assert not (keys / ".active").exists()
    assert not (keys / "astinuslab-default").exists()
    assert not (keys / "astinuslab-default.pub").exists()


def test_managed_config_restore_composes_with_exact_acl_rollback(tmp_path):
    install = tmp_path / "install"
    snapshot = tmp_path / "snapshot"
    (install / "config").mkdir(parents=True)
    snapshot.mkdir()
    config = install / "astinuslab.toml"
    config.write_bytes(b"[auth]\nadmin_token_hash = 'prior'\n")
    config.chmod(0o640)
    prior = config.stat()
    acl_snapshot = snapshot / "install.acl"

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        MIGRATION_SNAPSHOT={shlex.quote(str(snapshot))}
        snapshot_acl_state "$INSTALL_DIR" {shlex.quote(str(acl_snapshot))}
        snapshot_managed_config_state
        printf '%s\n' candidate > {shlex.quote(str(config))}
        chmod 0600 {shlex.quote(str(config))}
        restore_managed_config_state
        restore_acl_snapshot {shlex.quote(str(acl_snapshot))} "$INSTALL_DIR/storage"
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_bytes() == b"[auth]\nadmin_token_hash = 'prior'\n"
    restored = config.stat()
    assert restored.st_ino == prior.st_ino
    assert restored.st_mode & 0o777 == 0o640


def test_real_auth_normalization_preserves_rollback_inode_custody(tmp_path):
    install = tmp_path / "install"
    snapshot = tmp_path / "snapshot"
    (install / "config").mkdir(parents=True)
    snapshot.mkdir()
    config = install / "astinuslab.toml"
    env_file = install / "config/astinuslab.env"
    config_before = b'[auth]\nenabled = false\nadmin_api_token_hash = ""\n'
    env_before = b"ASTINUSLAB_AUTH_ENABLED=false\nASTINUSLAB_API_PORT=8000\n"
    config.write_bytes(config_before)
    env_file.write_bytes(env_before)
    config.chmod(0o640)
    env_file.chmod(0o640)
    identities = {path: (path.stat().st_dev, path.stat().st_ino) for path in (config, env_file)}
    acl_snapshot = snapshot / "install.acl"

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        MIGRATION_SNAPSHOT={shlex.quote(str(snapshot))}
        SERVICE_USER=$(id -un)
        snapshot_acl_state "$INSTALL_DIR" {shlex.quote(str(acl_snapshot))}
        snapshot_managed_config_state
        ADMIN_TOKEN_HASH=sha256:{"c" * 64}
        configure_admin_auth
        restore_managed_config_state
        restore_acl_snapshot {shlex.quote(str(acl_snapshot))} "$INSTALL_DIR/storage"
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_bytes() == config_before
    assert env_file.read_bytes() == env_before
    for path in (config, env_file):
        assert (path.stat().st_dev, path.stat().st_ino) == identities[path]
        assert path.stat().st_mode & 0o777 == 0o640


def test_existing_split_upgrade_reconciles_exact_permissions_only_after_quiesce():
    harness = f"""
    source {INSTALL}
    SPLIT_USERS=true
    migration_snapshot() {{ echo snapshot; }}
    install_source() {{ echo FORBIDDEN-install-source; }}
    prepare_storage() {{ echo FORBIDDEN-prepare-storage; }}
    install_split_upgrade_source() {{ echo stage-immutable; }}
    quiesce_services() {{ echo quiesce; }}
    migration_snapshot_mutable_state() {{ echo mutable-snapshot; }}
    backup_database_before_migration() {{ echo backup; }}
    resolve_storage_root() {{ echo resolve; }}
    bind_storage_root_identity() {{ echo bind; }}
    prepare_split_upgrade_mutable_state() {{ echo exact-split-permissions; }}
    apply_db_migrations() {{ echo migrations; }}
    handle_ssh_key() {{ echo ssh; }}
    assert_storage_root_identity() {{ echo identity; }}
    configure_mac() {{ echo mac; }}
    install_systemd_units() {{ echo units; }}
    finalize_split_sudoers() {{ echo sudoers; }}
    verify_final_runner_helpers() {{ echo helpers; }}
    switch_release_and_restart() {{ echo switch; }}
    verify_split_services() {{ echo readiness; }}
    migration_commit() {{ echo commit; }}
    cleanup_superseded_legacy_install() {{ echo cleanup; }}
    rollback_existing_split_upgrade() {{ echo FORBIDDEN-rollback; }}
    upgrade_existing_split_install
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert not any("FORBIDDEN" in line for line in lines)
    assert lines.index("quiesce") < lines.index("exact-split-permissions")
    assert lines.index("exact-split-permissions") < lines.index("migrations")


def test_upgrade_fixture_strips_inherited_source_permissions(tmp_path):
    install = tmp_path / "install"
    for relative in ("src/astinuslab", ".venv/lib", "ops"):
        (install / relative).mkdir(parents=True)
    source = install / "src/astinuslab"
    (source / "module.py").write_text("payload\n", encoding="utf-8")
    (install / ".venv/lib/site.py").write_text("payload\n", encoding="utf-8")
    (install / "ops/install.sh").write_text("payload\n", encoding="utf-8")
    for path in (source, install / ".venv/lib", install / "ops"):
        path.chmod(0o2775)
    subprocess.run(["setfacl", "-m", "u:daemon:rwx", str(source)], check=True)

    script = f"""
    source {shlex.quote(str(INSTALL))}
    INSTALL_DIR={shlex.quote(str(install))}
    RELEASE_DIR={shlex.quote(str(install))}
    SERVICE_USER=$(id -un)
    chown() {{ :; }}
    reconcile_install_permissions
    """
    result = run_bash(script)
    assert result.returncode == 0, result.stderr
    source_dirs = [path for path in install.rglob("*") if path.is_dir() and not path.is_symlink()]
    for path in source_dirs:
        mode = path.stat().st_mode
        assert mode & 0o020 == 0
        assert mode & 0o002 == 0
        assert mode & 0o2000 == 0
        acl = subprocess.run(["getfacl", "-cp", str(path)], capture_output=True, text=True, check=True).stdout
        assert "user:daemon:" not in acl
    # The files themselves, not just their directories: `runuser -u nobody` is
    # refused outright for a non-root caller, so probing with it would return
    # non-zero even against a world-writable file and prove nothing. Assert the
    # permission bits directly, and keep the real write attempt for a root run.
    for relative in ("src/astinuslab/module.py", ".venv/lib/site.py", "ops/install.sh"):
        probe = install / relative
        mode = probe.stat().st_mode
        assert mode & 0o022 == 0, f"{probe} is group or world writable"
        acl = subprocess.run(["getfacl", "-cp", str(probe)], capture_output=True, text=True, check=True).stdout
        assert "user:daemon:" not in acl

    if os.geteuid() == 0:
        for relative in ("src/astinuslab/module.py", ".venv/lib/site.py", "ops/install.sh"):
            probe = install / relative
            result = subprocess.run(
                ["runuser", "-u", "nobody", "--", "sh", "-c", f"echo x >> {probe}"],
                capture_output=True,
                text=True,
            )
            assert result.returncode != 0, f"nobody could write {probe}"


@pytest.mark.parametrize("source_umask", [0o022, 0o077], ids=["ordinary-source", "private-source"])
def test_immutable_release_reconciliation_restores_service_read_and_execute(tmp_path, source_umask):
    """Root-owned releases must stay usable after a private archive extraction."""
    _require_fakeroot()
    install = tmp_path / "install"
    release = install / "releases" / "candidate"
    module = release / "src/astinuslab/module.py"
    executable = release / ".venv/bin/entrypoint"
    for path, contents in ((module, "VALUE = 1\n"), (executable, "#!/bin/sh\nexit 0\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o666 & ~source_umask)
    executable.chmod(0o777 & ~source_umask)
    for path in [release, *release.rglob("*")]:
        if path.is_dir():
            path.chmod(0o777 & ~source_umask)
    credentials = ("astinuslab.toml", "config/astinuslab.env", "storage/ssh_keys/id_fixture")
    for relative in credentials:
        path = install / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private fixture\n", encoding="utf-8")
        path.chmod(0o600)

    result = run_fakeroot_bash(f"""
        set -euo pipefail
        source {shlex.quote(str(INSTALL))}
        INSTALL_DIR={shlex.quote(str(install))}
        RELEASE_DIR={shlex.quote(str(release))}
        SPLIT_USERS=false
        chown -R 1234:1234 "$RELEASE_DIR"
        [[ $(stat -c %u "$RELEASE_DIR") == 1234 ]]
        chown 4321:4321 "$INSTALL_DIR/astinuslab.toml" "$INSTALL_DIR/config/astinuslab.env" \
            "$INSTALL_DIR/storage/ssh_keys/id_fixture"
        reconcile_install_permissions true
        python3 - "$RELEASE_DIR" "$INSTALL_DIR" <<'PY'
import json
import sys
from pathlib import Path

release, install = map(Path, sys.argv[1:])
def state(path, relative):
    info = path.stat()
    return dict(path=relative, uid=info.st_uid, gid=info.st_gid, mode=info.st_mode & 0o7777, directory=path.is_dir())
print(json.dumps(dict(
    immutable=[state(path, str(path.relative_to(release))) for path in [release, *release.rglob("*")]],
    mutable=[state(install / relative, relative) for relative in
             ("astinuslab.toml", "config/astinuslab.env", "storage/ssh_keys/id_fixture")],
)))
PY
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    states = json.loads(result.stdout)
    for entry in states["immutable"]:
        assert (entry["uid"], entry["gid"]) == (0, 0), entry
        assert entry["mode"] & 0o022 == 0, entry
        assert entry["mode"] & 0o444 == 0o444, f"service cannot read immutable content: {entry}"
        if entry["directory"]:
            assert entry["mode"] & 0o111 == 0o111, f"service cannot traverse immutable directory: {entry}"
            assert entry["mode"] & 0o2000 == 0, entry
    modes = {entry["path"]: entry["mode"] for entry in states["immutable"]}
    assert modes["src/astinuslab/module.py"] == 0o644
    assert modes[".venv/bin/entrypoint"] == 0o755
    for entry in states["mutable"]:
        assert (entry["uid"], entry["gid"], entry["mode"]) == (4321, 4321, 0o600), entry


def test_umask_0007_keeps_sqlite_wal_and_shm_siblings_group_writable(tmp_path):
    import sqlite3

    db = tmp_path / "astinuslab.db"
    # The installer pre-creates each shared database at 0660. SQLite derives
    # WAL/SHM permissions from that database mode; UMask=0007 must preserve,
    # rather than strip, its group-write bit.
    db.touch(mode=0o660)
    db.chmod(0o660)
    previous = os.umask(0o007)
    try:
        connection = sqlite3.connect(db)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('x')")
        connection.commit()
        assert (tmp_path / "astinuslab.db-wal").exists()
        assert (tmp_path / "astinuslab.db-shm").exists()
        assert (tmp_path / "astinuslab.db-wal").stat().st_mode & 0o020
        assert (tmp_path / "astinuslab.db-shm").stat().st_mode & 0o020
        connection.close()
    finally:
        os.umask(previous)


def test_auth_flag_matrix_is_explicit():
    for flags in (
        "--generate-admin-token --admin-token-hash sha256:" + "a" * 64,
        "--generate-admin-token --allow-unauthenticated-local",
        "--admin-token-hash sha256:" + "a" * 64 + " --allow-unauthenticated-local",
    ):
        result = run_bash(f"source {shlex.quote(str(INSTALL))}; parse_args --install-dir /tmp/astinuslab-test {flags}")
        assert result.returncode != 0, flags


def test_clean_auth_config_generates_hash_and_prints_raw_token_once(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    result = run_bash(
        f"source {shlex.quote(str(INSTALL))}; INSTALL_DIR={shlex.quote(str(install))}; "
        "SERVICE_USER=$(id -un); configure_admin_auth",
    )
    assert result.returncode == 0, result.stderr
    config = (install / "astinuslab.toml").read_text(encoding="utf-8")
    assert "enabled = true" in config
    assert re.search(r'admin_api_token_hash = "sha256:[0-9a-f]{64}"', config)
    token_lines = [line for line in result.stdout.splitlines() if "Admin token:" in line]
    assert len(token_lines) == 1
    token = token_lines[0].split("Admin token: ", 1)[1]
    assert token and token not in config
    assert token not in result.stderr
    assert token not in " ".join(result.args)
    assert (install / "astinuslab.toml").stat().st_mode & 0o077 == 0


def test_generated_token_is_repeated_once_in_final_handoff_then_unset(tmp_path):
    install = tmp_path / "install"
    (install / "config").mkdir(parents=True)
    (install / "config/astinuslab.env").write_text("ASTINUSLAB_API_PORT=8123\n", encoding="utf-8")
    result = run_bash(
        f"""
        source {shlex.quote(str(INSTALL))}
        INSTALL_DIR={shlex.quote(str(install))}
        SERVICE_USER=$(id -un)
        RUNNER_USER="$SERVICE_USER"
        configure_admin_auth
        print_success
        printf 'TOKEN-STATE=%s\n' "${{GENERATED_ADMIN_TOKEN-UNSET}}"
        export -p
        """
    )

    assert result.returncode == 0, result.stdout + result.stderr
    token_lines = [line for line in result.stdout.splitlines() if "Admin token:" in line]
    assert len(token_lines) == 2
    assert token_lines[0].split("Admin token: ", 1)[1] == token_lines[1].split("Admin token: ", 1)[1]
    token = token_lines[0].split("Admin token: ", 1)[1]
    assert token not in "\n".join(path.read_text(encoding="utf-8") for path in install.rglob("*") if path.is_file())
    assert "TOKEN-STATE=UNSET" in result.stdout
    assert "declare -x GENERATED_ADMIN_TOKEN" not in result.stdout
    assert token not in result.stderr


def test_success_handoff_separates_required_and_optional_actions(tmp_path):
    install = tmp_path / "install"
    (install / "config").mkdir(parents=True)
    (install / "config/astinuslab.env").write_text("ASTINUSLAB_API_PORT=8123\n", encoding="utf-8")
    result = run_bash(
        f"""
        source {shlex.quote(str(INSTALL))}
        INSTALL_DIR={shlex.quote(str(install))}
        SERVICE_USER=operator
        RUNNER_USER=operator
        GENERATED_ADMIN_TOKEN=handoff-secret
        print_success
        """
    )

    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout
    assert "CREDENTIALS — SAVE NOW" in output
    assert "REQUIRED BEFORE THE FIRST VM" in output
    assert "OPTIONAL VOLATILITY 3 ANALYSIS" in output
    assert "Admin token: handoff-secret" in output
    assert "approximately 3 GB" in output
    assert "at least 10 GB free" in output
    assert f"cd {install}/current && sudo -u operator ./ops/images/download_images.sh" in output
    assert "./astinuslab.sh smoke --require-running" in output
    assert "./astinuslab.sh guide-volatility" in output
    assert "http://localhost:8123" in output


def test_existing_auth_hash_is_preserved_without_token_output(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    existing = ' [auth]\nenabled = true\nadmin_api_token_hash = "sha256:' + "b" * 64 + '"\n'
    (install / "astinuslab.toml").write_text(existing, encoding="utf-8")
    result = run_bash(
        f"source {shlex.quote(str(INSTALL))}; INSTALL_DIR={shlex.quote(str(install))}; "
        "SERVICE_USER=$(id -un); configure_admin_auth",
    )
    assert result.returncode == 0, result.stderr
    assert (install / "astinuslab.toml").read_text(encoding="utf-8") == existing
    assert "Admin token:" not in result.stdout


def test_no_auth_requires_explicit_development_flag(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    result = run_bash(
        f"source {shlex.quote(str(INSTALL))}; INSTALL_DIR={shlex.quote(str(install))}; "
        "SERVICE_USER=$(id -un); configure_admin_auth",
    )
    assert result.returncode == 0, result.stderr
    assert "enabled = true" in (install / "astinuslab.toml").read_text(encoding="utf-8")
    install_explicit = tmp_path / "install-explicit"
    install_explicit.mkdir()
    result = run_bash(
        f"source {shlex.quote(str(INSTALL))}; INSTALL_DIR={shlex.quote(str(install_explicit))}; "
        "SERVICE_USER=$(id -un); AUTH_ALLOW_UNAUTHENTICATED_LOCAL=true; configure_admin_auth",
    )
    assert result.returncode == 0, result.stderr
    assert "enabled = false" in (install_explicit / "astinuslab.toml").read_text(encoding="utf-8")


def test_runtime_config_defaults_preserve_existing_and_copy_missing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    install = tmp_path / "install"
    (repo / "config/catalog").mkdir(parents=True)
    (install / "config/catalog").mkdir(parents=True)
    (repo / "config").mkdir(exist_ok=True)

    (repo / "astinuslab.toml").write_text("repo toml\n", encoding="utf-8")
    (repo / "config/astinuslab.env.example").write_text("repo env\n", encoding="utf-8")
    (repo / "config/catalog/targets.example.yaml").write_text("repo target example\n", encoding="utf-8")
    (repo / "config/catalog/profiles.yaml").write_text("repo profiles\n", encoding="utf-8")
    (repo / "config/catalog/network_nodes.example.yaml").write_text("repo nodes\n", encoding="utf-8")

    configured_tools = """[vol3]
volatility_bin = "/operator/vol"
dwarf2json_path = "/operator/dwarf2json"
dwarf2json_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
"""
    (install / "astinuslab.toml").write_text(configured_tools, encoding="utf-8")
    (install / "config/astinuslab.env").write_text("user env\n", encoding="utf-8")
    (install / "config/catalog/targets.yaml").write_text("user targets\n", encoding="utf-8")
    (install / "config/catalog/profiles.yaml").write_text("user profiles\n", encoding="utf-8")

    script = f"""
    source {INSTALL}
    REPO_DIR={repo}
    INSTALL_DIR={install}
    SERVICE_USER=$(id -un)
    install_runtime_config_defaults
    """
    result = run_bash(script)
    assert result.returncode == 0, result.stderr
    assert (install / "astinuslab.toml").read_text(encoding="utf-8") == configured_tools
    assert (install / "config/astinuslab.env").read_text(encoding="utf-8") == "user env\n"
    assert (install / "config/catalog/targets.yaml").read_text(encoding="utf-8") == "user targets\n"
    assert (install / "config/catalog/profiles.yaml").read_text(encoding="utf-8") == "user profiles\n"
    assert (install / "config/catalog/network_nodes.yaml").read_text(encoding="utf-8") == "repo nodes\n"


def test_runtime_config_defaults_stops_on_a_middle_copy_failure():
    result = run_bash(f"""
        source {INSTALL}
        copy_calls=0
        validate_managed_config_destinations() {{ :; }}
        prepare_managed_directory() {{ :; }}
        install_config_if_missing() {{
          copy_calls=$((copy_calls + 1))
          [[ $copy_calls -ne 2 ]]
        }}
        secure_managed_file() {{ exit 97; }}
        secure_tree_permissions() {{ exit 98; }}
        if install_runtime_config_defaults; then exit 99; fi
        [[ $copy_calls -eq 2 ]]
    """)

    assert result.returncode == 0, result.stdout + result.stderr


def test_single_identity_upgrade_propagates_an_inner_release_staging_failure(tmp_path):
    events = tmp_path / "events"
    result = run_bash(f"""
        source {INSTALL}
        EVENTS={shlex.quote(str(events))}
        migration_snapshot() {{ :; }}
        stage_release() {{ echo inner-failure >> "$EVENTS"; return 42; }}
        rollback_existing_single_upgrade() {{ echo rollback >> "$EVENTS"; }}
        if upgrade_single_identity_install; then exit 99; fi
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == ["inner-failure", "rollback"]


def test_single_identity_upgrade_propagates_shared_database_preparation_failure(tmp_path):
    events = tmp_path / "events"
    result = run_bash(f"""
        source {INSTALL}
        EVENTS={shlex.quote(str(events))}
        migration_snapshot() {{ :; }}
        install_source() {{ :; }}
        quiesce_services() {{ :; }}
        migration_snapshot_mutable_state() {{ :; }}
        prepare_single_upgrade_mutable_state() {{ :; }}
        prepare_storage() {{ echo database-failure >> "$EVENTS"; return 42; }}
        rollback_existing_single_upgrade() {{ echo rollback >> "$EVENTS"; }}
        if upgrade_single_identity_install; then exit 99; fi
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == ["database-failure", "rollback"]


def test_existing_admin_token_branch_propagates_neutraliser_failure(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    (install / "astinuslab.toml").write_text(
        '[auth]\nadmin_api_token_hash = "sha256:' + ("a" * 64) + '"\n',
        encoding="utf-8",
    )

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        secure_admin_config() {{ :; }}
        neutralise_env_auth_overrides() {{ return 42; }}
        if configure_admin_auth; then exit 99; fi
    """)

    assert result.returncode == 0, result.stdout + result.stderr


def test_quiesce_stops_an_activating_managed_service(tmp_path):
    calls = tmp_path / "calls"
    result = run_bash(f"""
        source {INSTALL}
        SERVICE_USER=operator
        active_state=activating
        main_pid=4321
        systemctl() {{
          local action="$1" unit="$2"
          case "$action" in
                show)
                  case "$4" in
                    ActiveState)
                      [[ "$unit" == astinuslab-api@operator.service ]] \
                        && echo "$active_state" || echo inactive
                      ;;
                MainPID) [[ "$unit" == astinuslab-api@operator.service ]] && echo "$main_pid" || echo 0 ;;
              esac
              ;;
            stop)
              echo "stop $unit" >> {shlex.quote(str(calls))}
              active_state=inactive
              main_pid=0
              ;;
            *) return 2 ;;
          esac
        }}
        quiesce_services
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == ["stop astinuslab-api@operator.service"]


def test_installer_rejects_foreign_template_instances_before_mutation():
    result = run_bash(f"""
        source {INSTALL}
        SERVICE_USER=operator
        systemctl() {{
          case "$1" in
            list-units)
              printf '%s\n' \
                '● astinuslab-api@operator.service loaded failed failed managed' \
                'astinuslab-api@operator.service loaded active running owned' \
                'astinuslab-runner@other.service loaded activating start foreign'
              ;;
            list-unit-files)
              printf '%s\n' \
                'astinuslab-api@.service enabled' \
                'astinuslab-analysis@other.service enabled'
              ;;
          esac
        }}
        foreign_template_instances
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "astinuslab-analysis@other.service",
        "astinuslab-runner@other.service",
    ]

    text = INSTALL.read_text(encoding="utf-8")
    main = text[text.index("main()") :]
    assert main.index("refuse_foreign_template_instances") < main.index("check_prerequisites")


# systemd 255.4 (Ubuntu 24.04) exits 1 from `list-unit-files` when the supplied
# patterns match nothing — the state of every host a clean install runs on —
# while the same query without patterns exits 0. The stub reproduces exactly
# that split so the scan cannot be satisfied by asking with patterns again.
def _systemctl_unit_scan_stub(*, loaded="", unit_files="", list_units_rc=0, unit_files_rc=0):
    return f"""
        LOADED={shlex.quote(loaded)}
        UNIT_FILES={shlex.quote(unit_files)}
        LIST_UNITS_RC={list_units_rc}
        UNIT_FILES_RC={unit_files_rc}
        systemctl() {{
          local verb="$1" argument patterned=false
          shift
          for argument in "$@"; do
            case "$argument" in --*) ;; *) patterned=true ;; esac
          done
          case "$verb" in
            list-units)
              if ! $patterned; then return 9; fi
              if [[ -n "$LOADED" ]]; then printf '%s\\n' "$LOADED"; fi
              return "$LIST_UNITS_RC"
              ;;
            list-unit-files)
              if $patterned; then return 1; fi
              if [[ -n "$UNIT_FILES" ]]; then printf '%s\\n' "$UNIT_FILES"; fi
              return "$UNIT_FILES_RC"
              ;;
          esac
          return 2
        }}
    """


PRISTINE_UNIT_FILES = "\n".join(
    [
        "getty@.service                             enabled         enabled",
        "getty@tty1.service                         generated       -",
        "ssh.service                                enabled         enabled",
        "systemd-networkd.service                   enabled-runtime enabled",
        "user@1000.service                          static          -",
    ]
)


def test_foreign_template_scan_is_clean_on_a_pristine_host():
    result = run_bash(f"""
        source {INSTALL}
        SERVICE_USER=operator
        {_systemctl_unit_scan_stub(unit_files=PRISTINE_UNIT_FILES)}
        foreign_template_instances
        echo "scan-rc=$?"
        if refuse_foreign_template_instances; then echo refuse-ok; else echo refuse-failed; fi
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["scan-rc=0", "refuse-ok"]


def test_foreign_template_scan_ignores_unrelated_and_owned_units():
    unit_files = "\n".join(
        [
            PRISTINE_UNIT_FILES,
            # Bare templates carry no instance and are never foreign.
            "astinuslab-api@.service                    disabled        enabled",
            "astinuslab-runner@.service                 disabled        enabled",
            "astinuslab-analysis@.service               disabled        enabled",
            # This install's own instances.
            "astinuslab-api@operator.service            enabled         enabled",
            "astinuslab-runner@operator.service         enabled         enabled",
            "astinuslab-analysis@operator.service       enabled         enabled",
            # Names that merely resemble a managed instance.
            "notastinuslab-api@other.service            enabled         enabled",
            "astinuslab-api@other.service.bak           disabled        -",
            "astinuslab-exporter@other.service          enabled         enabled",
        ]
    )
    loaded = "\n".join(
        [
            "astinuslab-api@operator.service loaded active running AstinusLab API",
            "● astinuslab-runner@operator.service loaded failed failed AstinusLab runner",
        ]
    )
    result = run_bash(f"""
        source {INSTALL}
        SERVICE_USER=operator
        {_systemctl_unit_scan_stub(loaded=loaded, unit_files=unit_files)}
        foreign_template_instances
        echo "scan-rc=$?"
        if refuse_foreign_template_instances; then echo refuse-ok; else echo refuse-failed; fi
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["scan-rc=0", "refuse-ok"]


def test_foreign_template_scan_reports_loaded_and_enabled_foreign_instances():
    unit_files = "\n".join(
        [
            PRISTINE_UNIT_FILES,
            "astinuslab-api@operator.service            enabled         enabled",
            "astinuslab-analysis@other.service          enabled         enabled",
        ]
    )
    loaded = "\n".join(
        [
            "astinuslab-api@operator.service loaded active running AstinusLab API",
            "● astinuslab-api@intruder.service loaded failed failed AstinusLab API",
            "astinuslab-runner@other.service loaded activating start AstinusLab runner",
        ]
    )
    result = run_bash(f"""
        source {INSTALL}
        SERVICE_USER=operator
        {_systemctl_unit_scan_stub(loaded=loaded, unit_files=unit_files)}
        foreign_template_instances
        if refuse_foreign_template_instances; then echo refuse-ok; else echo refuse-refused; fi
    """)

    foreign = [
        "astinuslab-analysis@other.service",
        "astinuslab-api@intruder.service",
        "astinuslab-runner@other.service",
    ]
    assert result.returncode == 0, result.stdout + result.stderr
    reported = result.stdout.splitlines()
    assert reported[: len(foreign)] == foreign
    assert "Foreign AstinusLab template instances are loaded or enabled" in reported[len(foreign)]
    assert reported[len(foreign) + 1 :] == ["refuse-refused"]
    # The refusal names the offending instances on stderr so the operator can act.
    assert result.stderr.splitlines() == foreign


@pytest.mark.parametrize(("list_units_rc", "unit_files_rc"), [(1, 0), (0, 1), (0, 4)])
def test_foreign_template_scan_fails_closed_when_a_query_fails(list_units_rc, unit_files_rc):
    result = run_bash(f"""
        source {INSTALL}
        SERVICE_USER=operator
        {
        _systemctl_unit_scan_stub(
            unit_files=PRISTINE_UNIT_FILES,
            list_units_rc=list_units_rc,
            unit_files_rc=unit_files_rc,
        )
    }
        if foreign_template_instances; then echo scan-ok; else echo scan-failed; fi
        if refuse_foreign_template_instances; then echo refuse-ok; else echo refuse-refused; fi
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["scan-failed", "refuse-refused"]


@pytest.mark.parametrize(
    ("loaded", "unit_files", "list_units_rc", "unit_files_rc", "expected_error"),
    [
        pytest.param("", PRISTINE_UNIT_FILES, 0, 0, "", id="pristine"),
        pytest.param(
            "",
            PRISTINE_UNIT_FILES + "\nastinuslab-api.service enabled\nastinuslab-runner.service enabled"
            "\nastinuslab-analysis.service enabled",
            0,
            0,
            "",
            id="split-only",
        ),
        pytest.param(
            "● astinuslab-api@operator.service loaded failed failed owned",
            PRISTINE_UNIT_FILES + "\nastinuslab-api@.service disabled"
            "\nastinuslab-analysis@operator.service enabled"
            "\nnotastinuslab-api@other.service enabled\nastinuslab-api@other.service.bak disabled"
            "\nastinuslab-exporter@other.service enabled",
            0,
            0,
            "",
            id="owned-and-unrelated",
        ),
        pytest.param(
            "● astinuslab-runner@other.service loaded failed failed foreign",
            PRISTINE_UNIT_FILES,
            0,
            0,
            "unowned instance",
            id="loaded-foreign",
        ),
        pytest.param(
            "",
            PRISTINE_UNIT_FILES + "\nastinuslab-analysis@other.service enabled",
            0,
            0,
            "unowned instance",
            id="enabled-foreign",
        ),
        pytest.param("", PRISTINE_UNIT_FILES, 1, 0, "Could not enumerate", id="loaded-query-failed"),
        pytest.param("", PRISTINE_UNIT_FILES, 0, 1, "Could not enumerate", id="files-query-failed"),
        pytest.param("", PRISTINE_UNIT_FILES, 0, 4, "Could not enumerate", id="systemd-unavailable"),
    ],
)
def test_uninstall_template_scan_before_mutation(loaded, unit_files, list_units_rc, unit_files_rc, expected_error):
    # The uninstaller is linear: execute only its real scan and refusal block,
    # never source the complete destructive entry point on the test host.
    uninstall = UNINSTALL.read_text(encoding="utf-8")
    scan = uninstall[uninstall.index("foreign_template_instances() {") : uninstall.index("# Warn if no active/enabled")]
    result = run_bash(f"""
        set -euo pipefail
        SERVICE_USER=operator
        die() {{ echo "$*" >&2; exit 1; }}
        warn() {{ echo "$*" >&2; }}
        {
        _systemctl_unit_scan_stub(
            loaded=loaded,
            unit_files=unit_files,
            list_units_rc=list_units_rc,
            unit_files_rc=unit_files_rc,
        )
    }
        {scan}
        echo mutation-started
    """)

    if expected_error:
        assert result.returncode == 1
        assert expected_error in result.stderr
        assert "mutation-started" not in result.stdout
        if expected_error == "unowned instance":
            assert "@other.service" in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.splitlines() == ["mutation-started"]


def test_split_upgrade_refuses_symlinked_config_before_touching_target(tmp_path):
    install = tmp_path / "install"
    (install / "config/catalog").mkdir(parents=True)
    (install / "astinuslab.toml").write_text("[app]\n", encoding="utf-8")
    sentinel = tmp_path / "outside.env"
    sentinel.write_text("ASTINUSLAB_AUTH_ENABLED=true\n", encoding="utf-8")
    sentinel.chmod(0o644)
    (install / "config/astinuslab.env").symlink_to(sentinel)
    before = (sentinel.read_bytes(), sentinel.stat().st_mode, sentinel.stat().st_uid, sentinel.stat().st_gid)

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        SERVICE_USER=$(id -un)
        API_USER=$(id -un)
        SPLIT_USERS=true
        install_runtime_config_defaults
    """)

    assert result.returncode != 0
    assert (sentinel.read_bytes(), sentinel.stat().st_mode, sentinel.stat().st_uid, sentinel.stat().st_gid) == before
    assert (install / "config/astinuslab.env").is_symlink()


# Development residue and internal working material that must never reach an
# operator host. `.venv313` is the one that motivated the glob: the exclusion was
# the literal `.venv`, so any suffixed sibling copied a whole interpreter tree.
# `.github` and deliberately tracked `plans` reach clean archives. `.hermes` and
# `.superpowers` are untracked working-tree residue excluded in defense-in-depth.
DEV_RESIDUE_DIRS = (
    ".venv",
    ".venv313",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".claude",
    ".github",
    ".hermes",
    ".superpowers",
    "node_modules",
    "plans",
    "site",
    "graphify-out",
    "tools",
    "to-do",
    "troubleshooting",
    "testdata",
)


def _sync_exclude_arguments() -> list[str]:
    """The exact rsync exclude/include flags sync_source_tree passes.

    Read out of install.sh rather than restated here: a copy would drift, and a
    drifted copy would assert about a command nobody runs.
    """
    text = INSTALL.read_text(encoding="utf-8")
    start = text.index("rsync -a ")
    end = text.index('"$REPO_DIR/"', start)
    block = text[start:end]
    flags = re.findall(r"--(exclude|include)='([^']*)'", block)
    assert flags, "could not find rsync exclude/include flags in install.sh"
    return [f"--{kind}={pattern}" for kind, pattern in flags]


def test_sync_source_tree_excludes_development_residue(tmp_path):
    """None of this belongs on an operator host.

    Left unexcluded these copied roughly 446 MB of caches, a second virtualenv,
    built docs, and the internal planning and review material into $INSTALL_DIR.
    """
    if not shutil.which("rsync"):
        pytest.skip("rsync is required to exercise the sync path")
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    install = tmp_path / "install"
    for name in DEV_RESIDUE_DIRS:
        (repo / name).mkdir(parents=True)
        (repo / name / "residue.txt").write_text(f"{name} residue\n", encoding="utf-8")
    # A file the installed tree genuinely needs, to prove the sync ran at all.
    (repo / "src/astinuslab").mkdir(parents=True)
    (repo / "src/astinuslab/__init__.py").write_text("real source\n", encoding="utf-8")

    result = run_bash(f"source {INSTALL}; REPO_DIR={repo}; INSTALL_DIR={install}; sync_source_tree")

    assert result.returncode == 0, result.stderr
    assert (install / "src/astinuslab/__init__.py").read_text(encoding="utf-8") == "real source\n"
    leaked = [name for name in DEV_RESIDUE_DIRS if (install / name).exists()]
    assert not leaked, f"development residue reached the installed tree: {leaked}"


def test_sync_source_tree_ships_published_help_but_no_internal_docs(tmp_path):
    """The readiness banners link to docs/user/VOLATILITY3.md, so it must be installed.

    Internal plans/audits stay out of the installed tree, and a nested */docs
    directory elsewhere in the repo must still be excluded as before.
    """
    if not shutil.which("rsync"):
        pytest.skip("rsync is required to exercise the sync path")
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    install = tmp_path / "install"
    (repo / "docs/user").mkdir(parents=True)
    (repo / "docs/api").mkdir(parents=True)
    (repo / "docs/audits").mkdir(parents=True)
    (repo / "docs/audits/internal.md").write_text("internal")
    (repo / "docs/index.md").write_text("help index")
    (repo / "backend/somewhere/docs").mkdir(parents=True)
    (repo / "docs/user/VOLATILITY3.md").write_text("operator guide\n", encoding="utf-8")
    (repo / "docs/api/errors.md").write_text("api reference\n", encoding="utf-8")
    (repo / "backend/somewhere/docs/notes.md").write_text("nested\n", encoding="utf-8")

    result = run_bash(f"source {INSTALL}; REPO_DIR={repo}; INSTALL_DIR={install}; sync_source_tree")

    assert result.returncode == 0, result.stderr
    assert (install / "docs/user/VOLATILITY3.md").read_text(encoding="utf-8") == "operator guide\n"
    assert (install / "docs/api/errors.md").read_text() == "api reference\n"
    assert (install / "docs/index.md").read_text() == "help index"
    assert not (install / "docs/audits").exists()
    assert not (install / "backend/somewhere/docs").exists(), "nested docs dirs must stay excluded"


def test_sync_source_tree_excludes_mutable_runtime_config_but_refreshes_examples(tmp_path):
    if not shutil.which("rsync"):
        return
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    install = tmp_path / "install"
    (repo / "config/catalog").mkdir(parents=True)
    (repo / "vol3/bin").mkdir(parents=True)
    (install / "config/catalog").mkdir(parents=True)
    (repo / "astinuslab.toml").write_text("repo toml\n", encoding="utf-8")
    (repo / "config/astinuslab.env").write_text("repo env\n", encoding="utf-8")
    (repo / "config/catalog/targets.yaml").write_text("repo targets\n", encoding="utf-8")
    (repo / "config/catalog/targets.example.yaml").write_text("repo example v2\n", encoding="utf-8")
    (repo / "config/catalog/targets.yml").write_text("repository live yml targets\n", encoding="utf-8")
    (repo / "config/catalog/targets.example.yml").write_text("repo yml example v2\n", encoding="utf-8")
    (repo / "vol3/bin/dwarf2json").write_text("synthetic third-party sentinel\n", encoding="utf-8")
    (install / "astinuslab.toml").write_text("user toml\n", encoding="utf-8")
    (install / "config/astinuslab.env").write_text("user env\n", encoding="utf-8")
    (install / "config/catalog/targets.yaml").write_text("user targets\n", encoding="utf-8")
    (install / "config/catalog/targets.example.yaml").write_text("old example\n", encoding="utf-8")
    (install / "config/catalog/targets.yml").write_text("user yml targets\n", encoding="utf-8")
    (install / "config/catalog/targets.example.yml").write_text("old yml example\n", encoding="utf-8")

    result = run_bash(f"source {INSTALL}; REPO_DIR={repo}; INSTALL_DIR={install}; sync_source_tree")
    assert result.returncode == 0, result.stderr
    assert (install / "astinuslab.toml").read_text(encoding="utf-8") == "user toml\n"
    assert (install / "config/astinuslab.env").read_text(encoding="utf-8") == "user env\n"
    assert (install / "config/catalog/targets.yaml").read_text(encoding="utf-8") == "user targets\n"
    assert (install / "config/catalog/targets.example.yaml").read_text(encoding="utf-8") == "repo example v2\n"
    assert (install / "config/catalog/targets.yml").read_text(encoding="utf-8") == "user yml targets\n"
    assert (install / "config/catalog/targets.example.yml").read_text(encoding="utf-8") == "repo yml example v2\n"
    assert not (install / "vol3/bin/dwarf2json").exists(), (
        f"Forbidden installed path: {install / 'vol3/bin/dwarf2json'}"
    )


def test_sync_source_tree_comments_distinguish_archive_and_worktree_residue():
    text = INSTALL.read_text(encoding="utf-8")
    start = text.index("sync_source_tree()")
    sync_block = text[start : text.index("# Releases before", start)]

    assert ".github and deliberately tracked plans reach clean archive installs" in sync_block
    assert ".hermes and .superpowers are untracked working-tree residue" in sync_block
    assert "defense-in-depth" in sync_block


# Every path a7a7147 deleted from vol3/bin/. README.md matters as much as the
# binary: it opens "# Vendored dwarf2json" and records an expected sha256, so
# leaving it behind keeps asserting the vendoring claim that commit retracted.
LEGACY_VOL3_BIN_FILES = ("README.md", "dwarf2json-linux-amd64", "dwarf2json-linux-amd64.sha256")


def _seed_legacy_vol3_bin(install: Path) -> Path:
    """Pre-seed an install tree the way a pre-a7a7147 release left it."""
    legacy_dir = install / "vol3/bin"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "README.md").write_text(f"# Vendored dwarf2json\n\nExpected sha256: {'0' * 64}\n", encoding="utf-8")
    (legacy_dir / "dwarf2json-linux-amd64").write_bytes(b"synthetic redistributed executable\n")
    (legacy_dir / "dwarf2json-linux-amd64.sha256").write_text(f"{'0' * 64}  dwarf2json-linux-amd64\n", encoding="utf-8")
    return legacy_dir


def test_update_removes_legacy_vol3_bin_files_left_by_previous_installs(tmp_path):
    if not shutil.which("rsync"):
        pytest.skip("rsync is required to exercise the install/update path")
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    install = tmp_path / "install"
    (repo / "config").mkdir(parents=True)
    (repo / "astinuslab.toml.example").write_text("repo toml\n", encoding="utf-8")
    legacy_dir = _seed_legacy_vol3_bin(install)
    (install / "astinuslab.toml").write_text("user toml\n", encoding="utf-8")

    # Root-only ownership reconciliation is covered separately; this fixture
    # keeps the legacy cleanup property isolated from privilege requirements.
    script = f"""
    source {INSTALL}
    REPO_DIR={repo}
    INSTALL_DIR={install}
    SERVICE_USER=$(id -un)
    chown() {{ :; }}
    chmod() {{ :; }}
    find() {{ :; }}
    create_venv() {{ :; }}
    prepare_managed_directory() {{ mkdir -p "$INSTALL_DIR/$1"; }}
    secure_tree_permissions() {{ :; }}
    install_source
    cleanup_superseded_legacy_install
    """
    result = run_bash(script)

    assert result.returncode == 0, result.stderr
    for name in LEGACY_VOL3_BIN_FILES:
        assert not (legacy_dir / name).exists(), f"legacy file survived the update: {legacy_dir / name}"
    assert not legacy_dir.exists(), "emptied vol3/bin should be removed"
    installed_config = (install / "astinuslab.toml").read_text(encoding="utf-8")
    assert installed_config.startswith("user toml\n")
    assert "enabled = true" in installed_config
    assert re.search(r'admin_api_token_hash = "sha256:[0-9a-f]{64}"', installed_config)
    assert str(legacy_dir / LEGACY_VOL3_BIN_FILES[0]) in result.stdout


def test_legacy_vol3_bin_removal_is_scoped_and_keeps_operator_files(tmp_path):
    install = tmp_path / "install"
    legacy_dir = _seed_legacy_vol3_bin(install)
    operator_file = legacy_dir / "operator-supplied-notes.txt"
    operator_file.write_text("operator owns this directory\n", encoding="utf-8")

    result = run_bash(f"source {INSTALL}; INSTALL_DIR={install}; remove_legacy_vol3_bin_files")

    assert result.returncode == 0, result.stderr
    for name in LEGACY_VOL3_BIN_FILES:
        assert not (legacy_dir / name).exists()
    assert operator_file.read_text(encoding="utf-8") == "operator owns this directory\n"
    assert legacy_dir.is_dir(), "a non-empty operator directory must never be removed"


def test_legacy_vol3_bin_removal_is_a_silent_no_op_on_clean_installs(tmp_path):
    install = tmp_path / "install"
    (install / "vol3/bin").mkdir(parents=True)

    result = run_bash(f"source {INSTALL}; INSTALL_DIR={install}; remove_legacy_vol3_bin_files")

    assert result.returncode == 0, result.stderr
    assert "Removing legacy" not in result.stdout
    assert (install / "vol3/bin").is_dir(), "an operator directory must not be removed when nothing was cleaned up"


def _validate_install_dir_body(script: Path) -> str:
    match = re.search(r"^validate_install_dir\(\) \{\n(.*?)^\}$", script.read_text(encoding="utf-8"), re.M | re.S)
    assert match, f"{script} does not define validate_install_dir()"
    return match.group(1)


def test_install_and_uninstall_share_one_install_dir_validator():
    """The two copies must not drift.

    They are duplicated on purpose — both scripts are run standalone by
    operators, so neither may depend on sourcing a shared snippet — which only
    works as a safety property while the copies stay identical.
    """
    assert _validate_install_dir_body(UNINSTALL) == _validate_install_dir_body(INSTALL)
    # A definition nothing calls would satisfy the comparison above.
    assert re.search(r"^validate_install_dir$", UNINSTALL.read_text(encoding="utf-8"), re.M)


@pytest.mark.parametrize("unsafe_dir", UNSAFE_INSTALL_DIRS)
def test_install_rejects_unsafe_install_dir(unsafe_dir):
    result = run_bash(f"source {INSTALL}; INSTALL_DIR={shlex.quote(unsafe_dir)}; validate_install_dir")

    assert result.returncode != 0, f"install.sh accepted {unsafe_dir!r}"
    assert "INSTALL_DIR" in result.stderr


@pytest.mark.parametrize("unsafe_dir", UNSAFE_INSTALL_DIRS)
def test_uninstall_rejects_every_install_dir_the_installer_rejects(unsafe_dir):
    """`INSTALL_DIR=/usr sudo bash ops/deploy/uninstall.sh` must never reach `rm -rf`.

    Safe to execute for real: validation runs before the root check and before
    any systemctl/filesystem work, so a rejected value exits on the first
    validation line having touched nothing.
    """
    result = subprocess.run(
        ["bash", str(UNINSTALL)],
        cwd=ROOT,
        env={**os.environ, "INSTALL_DIR": unsafe_dir},
        capture_output=True,
        text=True,
        input="",
    )

    assert result.returncode != 0, f"uninstall.sh accepted {unsafe_dir!r}"
    assert "INSTALL_DIR" in result.stderr
    assert "Delete" not in result.stdout, "uninstall.sh reached the removal prompt for an unsafe INSTALL_DIR"


def test_uninstall_accepts_the_default_install_dir():
    """Positive control: the validator must not reject the path we actually ship.

    Only meaningful as an unprivileged user, where the root check right after
    validation stops the run before anything is touched.
    """
    if os.geteuid() == 0:
        pytest.skip("would run the real uninstaller as root")

    result = subprocess.run(
        ["bash", str(UNINSTALL)],
        cwd=ROOT,
        env={**os.environ, "INSTALL_DIR": "/opt/astinuslab"},
        capture_output=True,
        text=True,
        input="",
    )

    assert result.returncode != 0
    assert "Run this script as root" in result.stderr
    assert "INSTALL_DIR" not in result.stderr


def test_uninstall_removes_legacy_vol3_bin_files():
    uninstall = (ROOT / "ops/deploy/uninstall.sh").read_text(encoding="utf-8")
    privileged_state = (ROOT / "ops/deploy/privileged_state.py").read_text(encoding="utf-8")

    assert "clean-legacy-vol3-bin" in uninstall
    for name in LEGACY_VOL3_BIN_FILES:
        assert f'"{name}"' in privileged_state
    # Any recursive wipe of the now operator-owned directory, however quoted.
    assert not re.search(
        r"rm\s+-[a-zA-Z]*r[a-zA-Z]*\s+(--\s+)?\"\$(INSTALL_DIR/vol3/bin|LEGACY_VOL3_BIN_DIR)\"", uninstall
    )
    assert "os.unlink(name, dir_fd=bin_fd)" in privileged_state
    assert 'os.rmdir("bin", dir_fd=vol3_fd)' in privileged_state
    assert "O_NOFOLLOW" in privileged_state


def test_uninstall_recognizes_split_units_and_service_users():
    uninstall = UNINSTALL.read_text(encoding="utf-8")
    for unit in ("astinuslab-api.service", "astinuslab-runner.service", "astinuslab-analysis.service"):
        assert unit in uninstall
    for user in ("astinuslab-api", "astinuslab-runner", "astinuslab-analysis"):
        assert user in uninstall
    split_cleanup = uninstall[
        uninstall.index("if $SPLIT_USERS; then", uninstall.index("Optionally remove service user")) :
    ]
    assert "Delete split service user '$split_user'" in split_cleanup
    assert '[[ "${yn,,}" == "y" ]]' in split_cleanup
    assert split_cleanup.index("read -rp") < split_cleanup.index('userdel "$split_user"')


def test_legacy_vol3_bin_removal_refuses_to_follow_a_symlinked_parent(tmp_path):
    """A symlinked vol3/bin must never let the cleanup delete files outside the tree.

    An operator who kept tools in /opt/forensics-tools and symlinked vol3/bin at
    them under the old bundled layout has their own files sitting at exactly the
    legacy names. Testing only the leaf traverses the symlink and deletes them.
    """
    install = tmp_path / "install"
    (install / "vol3").mkdir(parents=True)
    operator_dir = tmp_path / "operator-tools"
    operator_dir.mkdir()
    for name in LEGACY_VOL3_BIN_FILES:
        (operator_dir / name).write_text("operator owns this\n", encoding="utf-8")
    (install / "vol3/bin").symlink_to(operator_dir, target_is_directory=True)

    result = run_bash(f"source {INSTALL}; INSTALL_DIR={install}; remove_legacy_vol3_bin_files")

    assert result.returncode == 0, result.stderr
    for name in LEGACY_VOL3_BIN_FILES:
        assert (operator_dir / name).read_text(encoding="utf-8") == "operator owns this\n", (
            f"cleanup escaped the install tree and deleted {operator_dir / name}"
        )
    assert (install / "vol3/bin").is_symlink(), "the operator's symlink must be left alone"


def test_legacy_vol3_cleanup_stays_on_bound_directory_during_parent_rename(tmp_path):
    install = tmp_path / "install"
    original = _seed_legacy_vol3_bin(install)
    outside = tmp_path / "outside"
    outside.mkdir()
    for name in LEGACY_VOL3_BIN_FILES:
        (outside / name).write_text("outside sentinel\n", encoding="utf-8")

    # Wrap the shared cleanup module and inject the exact rename after its bin
    # dirfd is bound. The cleanup must continue relative to that fd and must
    # never traverse the replacement symlink. The module is passed as a path
    # now rather than piped in, so the shim reads it from argv.
    result = run_bash(
        f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        export RACE_OUTSIDE={shlex.quote(str(outside))}
        python3() {{
          command python3 -c '
import os, sys
source = open(sys.argv[1], encoding="utf-8").read()
needle = "        bin_info = os.fstat(bin_fd)"
injected = needle + "\\n        os.rename(\\\"bin\\\", \\\"bin-bound\\\", src_dir_fd=vol3_fd, dst_dir_fd=vol3_fd)" + "\\n        os.symlink(os.environ[\\\"RACE_OUTSIDE\\\"], \\\"bin\\\", dir_fd=vol3_fd)"
if needle not in source:
    raise SystemExit("race injection point missing")
sys.argv = sys.argv[1:]
exec(compile(source.replace(needle, injected, 1), "<legacy-cleanup-race>", "exec"))
' "$@"
        }}
        remove_legacy_vol3_bin_files
        """
    )

    assert result.returncode == 0, result.stdout + result.stderr
    for name in LEGACY_VOL3_BIN_FILES:
        assert (outside / name).read_text(encoding="utf-8") == "outside sentinel\n"
        assert not (install / "vol3/bin-bound" / name).exists()
    assert (install / "vol3/bin").is_symlink()
    assert original.with_name("bin-bound").is_dir()


def test_install_and_uninstall_legacy_cleanup_use_descriptor_relative_unlink():
    """Both entry points clean the legacy directory, and must do it the same way.

    They used to hold a copy each, and this compared the two texts. They now run
    one implementation, so what is worth pinning is that neither has grown its
    own copy back and that the shared one is still descriptor-relative.
    """
    privileged_state = (ROOT / "ops/deploy/privileged_state.py").read_text(encoding="utf-8")
    assert "os.unlink(name, dir_fd=bin_fd)" in privileged_state
    assert 'os.rmdir("bin", dir_fd=vol3_fd)' in privileged_state
    assert "O_NOFOLLOW" in privileged_state
    for script in (INSTALL, UNINSTALL):
        text = script.read_text(encoding="utf-8")
        assert "clean-legacy-vol3-bin" in text
        assert "dir_fd=bin_fd" not in text, f"{script.name} grew its own copy of the legacy cleanup back"


def test_legacy_vol3_bin_removal_reports_what_it_could_not_clean(tmp_path):
    """rmdir failing means residue remains — never report the directory as cleaned."""
    install = tmp_path / "install"
    legacy_dir = _seed_legacy_vol3_bin(install)
    (legacy_dir / "operator-supplied-notes.txt").write_text("keep me\n", encoding="utf-8")

    result = run_bash(f"source {INSTALL}; INSTALL_DIR={install}; remove_legacy_vol3_bin_files")

    assert result.returncode == 0, result.stderr
    assert "still contains" in result.stdout, result.stdout
    assert "operator-supplied-notes.txt" in result.stdout
    assert "Legacy third-party files removed from" not in result.stdout, (
        "must not claim the directory was cleaned while residue remains"
    )


def test_help_works_without_root_and_lists_flags():
    result = subprocess.run([str(INSTALL), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    for flag in [
        "--install-dir",
        "--service-user",
        "--split-users",
        "--yes",
        "--generate-ssh-key",
        "--no-ssh-key",
        "--no-enable",
        "--no-start",
        "--restart",
        "--check",
    ]:
        assert flag in result.stdout


def test_parse_args_rejects_relative_install_dir_and_conflicts():
    result = run_bash(f"source {INSTALL}; parse_args --install-dir relative/path")
    assert result.returncode != 0
    assert "absolute path" in result.stderr
    result = run_bash(f"source {INSTALL}; parse_args --restart --no-start")
    assert result.returncode != 0
    assert "--restart conflicts with --no-start" in result.stderr

    result = run_bash(f"source {INSTALL}; parse_args --split-users --no-start")
    assert result.returncode != 0
    assert "--split-users requires starting and verifying the new services" in result.stderr
    result = run_bash(f"source {INSTALL}; parse_args --generate-ssh-key --no-ssh-key")
    assert result.returncode != 0
    assert "--generate-ssh-key conflicts with --no-ssh-key" in result.stderr


def test_split_users_is_explicit_and_default_keeps_operator_identity():
    default = run_bash(
        f"source {INSTALL}; SERVICE_USER=operator; parse_args --install-dir /tmp/astinuslab-test; "
        'printf "%s %s %s %s\\n" "$SPLIT_USERS" "$API_USER" "$RUNNER_USER" "$ANALYSIS_USER"'
    )
    assert default.returncode == 0, default.stderr
    assert default.stdout.strip() == "false operator operator operator"

    split = run_bash(
        f"source {INSTALL}; SERVICE_USER=operator; parse_args --install-dir /tmp/astinuslab-test --split-users; "
        'printf "%s %s %s %s\\n" "$SPLIT_USERS" "$API_USER" "$RUNNER_USER" "$ANALYSIS_USER"'
    )
    assert split.returncode == 0, split.stderr
    assert split.stdout.strip() == "true astinuslab-api astinuslab-runner astinuslab-analysis"


def test_split_install_rerun_requires_the_explicit_flag(tmp_path):
    marker = tmp_path / "runtime-identities"
    marker.write_text(
        "mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n",
        encoding="utf-8",
    )
    result = run_bash(
        f"source {INSTALL}; RUNTIME_IDENTITIES_FILE={marker}; "
        "split_identity_marker_is_valid() { return 0; }; "
        "parse_args --install-dir /tmp/astinuslab-test"
    )
    assert result.returncode != 0
    assert "existing split-user install requires --split-users" in result.stderr


def test_runtime_marker_authority_validator_is_identical_in_every_privileged_entrypoint():
    def function_body(path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        start = text.index("split_identity_marker_is_valid() {")
        end = text.index("\n}\n", start) + 3
        return text[start:end]

    bodies = {
        function_body(INSTALL),
        function_body(UNINSTALL),
        function_body(PRIVILEGED_ROOTS),
    }
    assert len(bodies) == 1, "install, uninstall and configure must enforce one exact marker authority shape"


def test_missing_split_marker_with_explicit_units_fails_closed(tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / "astinuslab-api.service").write_text("[Service]\nUser=astinuslab-api\n", encoding="utf-8")
    marker = tmp_path / "missing-runtime-identities"

    result = run_bash(
        f"source {INSTALL}; "
        f"SYSTEMD_UNIT_DIR={shlex.quote(str(unit_dir))}; "
        f"RUNTIME_IDENTITIES_FILE={shlex.quote(str(marker))}; "
        "parse_args --install-dir /tmp/astinuslab-test"
    )

    assert result.returncode != 0
    assert "incomplete or malformed split-user state" in result.stderr


def test_split_identity_creation_grants_hypervisor_groups_only_to_runner():
    harness = f"""
    source {INSTALL}
    getent() {{
      [[ "$1" == group && "$2" == astinuslab ]] && return 1
      [[ "$1" == group && ( "$2" == libvirt || "$2" == kvm ) ]] && return 0
      return 1
    }}
    id() {{ return 1; }}
    groupadd() {{ echo groupadd "$@"; }}
    useradd() {{ echo useradd "$@"; }}
    usermod() {{ echo usermod "$@"; }}
    ensure_split_users
    """
    result = run_bash(harness)
    assert result.returncode == 0, result.stderr
    assert "useradd --system --no-create-home --shell /usr/sbin/nologin -g astinuslab astinuslab-api" in result.stdout
    assert (
        "useradd --system --no-create-home --shell /usr/sbin/nologin -g astinuslab astinuslab-runner" in result.stdout
    )
    assert (
        "useradd --system --no-create-home --shell /usr/sbin/nologin -g astinuslab astinuslab-analysis" in result.stdout
    )
    assert "usermod -aG libvirt,kvm astinuslab-runner" in result.stdout
    assert "usermod -aG libvirt,kvm astinuslab-api" not in result.stdout
    assert "usermod -aG libvirt,kvm astinuslab-analysis" not in result.stdout


def test_split_identity_creation_refuses_incompatible_preexisting_accounts_without_mutation():
    harness = f"""
    source {INSTALL}
    API_USER=astinuslab-api
    RUNNER_USER=astinuslab-runner
    ANALYSIS_USER=astinuslab-analysis
    getent() {{ return 0; }}
    id() {{
      if [[ "$1" == -gn ]]; then echo legacy-group; return 0; fi
      if [[ "$1" == -nG ]]; then echo legacy-group libvirt; return 0; fi
      return 0
    }}
    usermod() {{ echo FORBIDDEN-user-mutation "$@"; }}
    if ensure_split_users; then exit 99; fi
    """
    result = run_bash(harness)
    assert result.returncode == 0, result.stderr
    assert "incompatible pre-existing identity" in result.stderr


def test_split_user_creation_propagates_group_creation_failure():
    harness = f"""
    source {INSTALL}
    getent() {{ return 1; }}
    groupadd() {{ return 42; }}
    id() {{
      case "${{1:-}}" in
        -gn) echo astinuslab ;;
        -nG) echo astinuslab ;;
      esac
      return 0
    }}
    if ensure_split_users; then exit 99; fi
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr
    assert "FORBIDDEN-user-mutation" not in result.stdout


def test_prepare_storage_ships_archive_omitted_symbols_and_shared_sqlite_modes(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    harness = f"""
    source {INSTALL}
    INSTALL_DIR={shlex.quote(str(install))}
    RELEASE_DIR={shlex.quote(str(install))}
    SERVICE_USER=$(id -un)
    SPLIT_USERS=true
    API_USER=$(id -un)
    RUNNER_USER=$(id -un)
    ANALYSIS_USER=$(id -un)
    chown() {{ :; }}
    chgrp() {{ :; }}
    prepare_storage
    prepare_shared_databases
    """
    result = run_bash(harness)
    assert result.returncode == 0, result.stderr
    assert (install / "vol3/symbols").is_dir()
    assert (install / "vol3/symbols").stat().st_mode & 0o7777 == 0o2770
    assert install.joinpath("storage").stat().st_mode & 0o7777 == 0o2770
    for database in ("astinuslab.db", "celery_broker.db", "celery_results.db"):
        path = install / "storage" / database
        assert path.exists(), database
        assert path.stat().st_mode & 0o777 == 0o660


def test_prepare_shared_databases_rejects_symlinks_without_touching_target(tmp_path):
    install = tmp_path / "install"
    storage = install / "storage"
    storage.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("do not mutate\n", encoding="utf-8")
    victim.chmod(0o644)
    (storage / "astinuslab.db-wal").symlink_to(victim)
    harness = f"""
    source {INSTALL}
    INSTALL_DIR={shlex.quote(str(install))}
    RELEASE_DIR={shlex.quote(str(install))}
    API_USER=$(id -un)
    if prepare_shared_databases; then exit 99; fi
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr
    assert victim.read_text(encoding="utf-8") == "do not mutate\n"
    assert victim.stat().st_mode & 0o777 == 0o644


def test_prepare_shared_databases_ignores_unrelated_db_names(tmp_path):
    install = tmp_path / "install"
    storage = install / "storage"
    storage.mkdir(parents=True)
    unrelated = storage / "attacker.db"
    unrelated.write_text("not a service database\n", encoding="utf-8")
    unrelated.chmod(0o400)
    harness = f"""
    source {INSTALL}
    INSTALL_DIR={shlex.quote(str(install))}
    RELEASE_DIR={shlex.quote(str(install))}
    API_USER=$(id -un)
    prepare_shared_databases
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr
    assert unrelated.stat().st_mode & 0o777 == 0o400


def test_split_permissions_preserve_evidence_and_narrow_mutable_paths(tmp_path):
    install = tmp_path / "install"
    for path in (
        "src",
        "ops",
        "tools",
        "deploy",
        "guest",
        ".venv",
        "config",
        "storage/dumps/job-1",
        "storage/ssh_keys",
        "storage/playground/disks",
        "storage/playground/captures/job-1",
        "images/definitions",
        "vol3/symbols",
        "docs",
    ):
        (install / path).mkdir(parents=True, exist_ok=True)
    (install / "README.md").write_text("source\n", encoding="utf-8")
    (install / "docs/index.md").write_text("docs\n", encoding="utf-8")
    (install / "astinuslab.toml").write_text("[app]\n", encoding="utf-8")
    (install / "config/astinuslab.env").write_text("ENV=1\n", encoding="utf-8")
    evidence = install / "storage/dumps/job-1/evidence.bin"
    evidence.write_bytes(b"evidence")
    evidence.chmod(0o440)
    capture = install / "storage/playground/captures/job-1/capture.bin"
    capture.write_bytes(b"capture")
    capture.chmod(0o440)
    ssh_key = install / "storage/ssh_keys/id_ed25519"
    ssh_key.write_text("key\n", encoding="utf-8")
    ssh_key.chmod(0o600)
    image = install / "images/definitions/base.img"
    image.write_bytes(b"image")
    image.chmod(0o640)
    symbol = install / "vol3/symbols/kernel.json"
    symbol.write_text("{}\n", encoding="utf-8")
    symbol.chmod(0o600)
    symbol_lock = install / "vol3/symbols/.lock-kernel"
    symbol_lock.touch(mode=0o640)
    helper_dir = tmp_path / "helpers"
    helper_dir.mkdir()
    fake_chown = helper_dir / "chown"
    fake_chown.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_chown.chmod(0o755)
    harness = f"""
    source {INSTALL}
    # This fixture uses one UID; real distinct-UID ACL proof is in test_runtime_control_locks.py.
    secure_runtime_control_lock_permissions() {{ :; }}
    secure_package_storage_permissions() {{ :; }}
    provision_service_logs() {{ :; }}
    INSTALL_DIR={shlex.quote(str(install))}
    RELEASE_DIR={shlex.quote(str(install))}
    API_USER=$(id -un)
    RUNNER_USER=$(id -un)
    ANALYSIS_USER=$(id -un)
    configure_split_permissions
    configure_split_permissions
    """

    result = run_bash(harness, env={"PATH": f"{helper_dir}:{os.environ['PATH']}"})

    assert result.returncode == 0, result.stderr
    assert evidence.stat().st_mode & 0o777 == 0o440
    assert capture.stat().st_mode & 0o777 == 0o440
    assert ssh_key.stat().st_mode & 0o777 == 0o640
    assert image.stat().st_mode & 0o777 == 0o640
    assert symbol.stat().st_mode & 0o777 == 0o660
    assert symbol_lock.stat().st_mode & 0o777 == 0o660
    assert install.joinpath("vol3/symbols").stat().st_mode & 0o7777 == 0o2770
    assert install.joinpath("storage/ssh_keys").stat().st_mode & 0o7777 == 0o2750


def test_libvirt_image_access_is_read_only_and_effective(tmp_path):
    definitions = tmp_path / "install/images/definitions"
    definitions.mkdir(parents=True)
    image = definitions / "base.img"
    image.write_bytes(b"image")
    definitions.chmod(0o2770)
    image.chmod(0o640)
    harness = f"""
    source {INSTALL}
    INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
    SERVICE_USER=$(id -un)
    API_USER=$(id -un)
    RUNNER_USER=$(id -un)
    ANALYSIS_USER=$(id -un)
    secure_libvirt_image_access
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr
    directory_acl = subprocess.run(["getfacl", "-cp", definitions], check=True, capture_output=True, text=True).stdout
    image_acl = subprocess.run(["getfacl", "-cp", image], check=True, capture_output=True, text=True).stdout
    assert "user:libvirt-qemu:r-x" in directory_acl
    assert "user:libvirt-qemu:r--" in image_acl
    assert "user:libvirt-qemu:rwx" not in directory_acl
    assert "user:libvirt-qemu:rw" not in image_acl


def test_image_acl_reconciliation_runs_in_both_identity_modes():
    text = INSTALL.read_text(encoding="utf-8")
    prepare = text[text.index("prepare_storage()") : text.index("prepare_managed_directory()")]
    split = text[text.index("configure_split_permissions()") : text.index("remove_legacy_evidence_acls()")]

    assert "secure_libvirt_image_access" in prepare
    assert "secure_libvirt_image_access" in split


def test_transitional_qemu_custody_covers_exactly_the_trees_qemu_can_write():
    """qemu legitimately owns files it created in the trees it may write.

    A real VM run leaves the seed ISO owned by libvirt-qemu under the playground
    disk tree, and a preserved base image the same way. Every other mutable tree
    must still refuse foreign custody.
    """
    text = INSTALL.read_text(encoding="utf-8")
    secure_tree = text[text.index("secure_tree_permissions()") : text.index("secure_ssh_key_permissions()")]
    # The image-ACL body is ops/deploy/permissions.py since the heredoc
    # extraction; the invariant it carries did not move with it.
    permissions = PERMISSIONS.read_text(encoding="utf-8")
    image_acl = permissions[
        permissions.index("def secure_libvirt_images(") : permissions.index("def secure_directory_root(")
    ]
    normalized = re.sub(r"\\\n\s*", " ", text)
    calls = {}
    for line in normalized.splitlines():
        stripped = line.strip()
        if not stripped.startswith("secure_tree_permissions "):
            continue
        arguments = shlex.split(stripped.split("||", 1)[0])[1:]
        calls[arguments[0]] = arguments

    assert 'extra_owner="${6:-}"' in secure_tree
    assert '"$extra_owner"' in secure_tree
    assert "allowed_owners.add(pwd.getpwnam(qemu_name).pw_uid)" in image_acl
    assert len(calls) >= 6, calls
    assert {root for root, arguments in calls.items() if "libvirt-qemu" in arguments} == {
        "$INSTALL_DIR/images/definitions",
        "$root/playground/disks",
    }


def test_secure_mutable_tree_admits_only_the_explicitly_named_extra_owner(tmp_path):
    # The invoking uid stands in for transitional libvirt-qemu custody: none of
    # the four runtime identities resolve, so the sixth argument is the only
    # thing that can admit the tree. Creating a genuinely libvirt-qemu-owned
    # entry needs root, so that half stays a real-host proof.
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    image = definitions / "base.img"
    image.write_bytes(b"image")
    definitions.chmod(0o2770)
    image.chmod(0o600)
    harness = """
    source {install}
    SERVICE_USER=astinuslab-absent-service
    API_USER=astinuslab-absent-api
    RUNNER_USER=astinuslab-absent-runner
    ANALYSIS_USER=astinuslab-absent-analysis
    secure_tree_permissions {tree} $(id -un) $(id -gn) 0750 0640 {extra}
    """

    refused = run_bash(harness.format(install=INSTALL, tree=shlex.quote(str(definitions)), extra=""))
    unchanged_directory = definitions.stat().st_mode & 0o7777
    unchanged_image = image.stat().st_mode & 0o777
    admitted = run_bash(harness.format(install=INSTALL, tree=shlex.quote(str(definitions)), extra="$(id -un)"))

    assert refused.returncode != 0
    assert "foreign custody" in refused.stderr
    assert unchanged_directory == 0o2770
    assert unchanged_image == 0o600
    assert admitted.returncode == 0, admitted.stderr
    assert definitions.stat().st_mode & 0o7777 == 0o750
    assert image.stat().st_mode & 0o777 == 0o640


def test_split_ssh_keys_grant_runner_only_named_read_acl(tmp_path):
    keys = tmp_path / "install/storage/ssh_keys"
    keys.mkdir(parents=True)
    private_key = keys / "id_ed25519"
    private_key.write_text("private\n", encoding="utf-8")
    harness = f"""
    source {INSTALL}
    INSTALL_DIR={tmp_path / "install"}
    API_USER=$(id -un)
    RUNNER_USER=libvirt-qemu
    secure_ssh_key_permissions
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr
    directory_acl = subprocess.run(["getfacl", "-cp", keys], check=True, capture_output=True, text=True).stdout
    private_acl = subprocess.run(["getfacl", "-cp", private_key], check=True, capture_output=True, text=True).stdout
    assert "group::---" in directory_acl
    assert "user:libvirt-qemu:r-x" in directory_acl
    assert "group::---" in private_acl
    assert "user:libvirt-qemu:r--" in private_acl


def test_split_logs_use_custody_helper_instead_of_recursive_normalization():
    text = INSTALL.read_text(encoding="utf-8")
    function = text[text.index("configure_split_storage_permissions()") : text.index("remove_legacy_evidence_acls()")]

    assert 'provision_service_logs "$root"' in function
    assert 'secure_tree_permissions "$root/logs"' not in function


def test_split_permission_source_normalization_includes_top_level_and_docs():
    text = INSTALL.read_text(encoding="utf-8")
    function = text[text.index("configure_split_permissions()") : text.index("verify_split_services()")]
    assert 'find "$INSTALL_DIR"' in function
    for mutable in ("config", "storage", "images/definitions", "vol3/symbols"):
        assert f'"$INSTALL_DIR/{mutable}"' in function
    assert "-prune" in function
    assert "chown -h root:root" in function


def test_split_permission_normalization_does_not_follow_source_symlinks(tmp_path):
    install = tmp_path / "install"
    for path in ("config", "storage/ssh_keys", "images/definitions", "vol3/symbols"):
        (install / path).mkdir(parents=True, exist_ok=True)
    (install / "astinuslab.toml").write_text("[app]\n", encoding="utf-8")
    victim = tmp_path / "victim"
    victim.write_text("outside\n", encoding="utf-8")
    victim.chmod(0o666)
    (install / "source-link").symlink_to(victim)
    helper_dir = tmp_path / "helpers"
    helper_dir.mkdir()
    fake_chown = helper_dir / "chown"
    fake_chown.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_chown.chmod(0o755)
    harness = f"""
    source {INSTALL}
    # This fixture uses one UID; real distinct-UID ACL proof is in test_runtime_control_locks.py.
    secure_runtime_control_lock_permissions() {{ :; }}
    secure_package_storage_permissions() {{ :; }}
    provision_service_logs() {{ :; }}
    INSTALL_DIR={install}
    API_USER=$(id -un)
    RUNNER_USER=$(id -un)
    ANALYSIS_USER=$(id -un)
    configure_split_permissions
    """

    result = run_bash(harness, env={"PATH": f"{helper_dir}:{os.environ['PATH']}"})

    assert result.returncode == 0, result.stderr
    assert victim.stat().st_mode & 0o777 == 0o666


def test_split_permissions_reject_symlinked_mutable_top_directory(tmp_path):
    install = tmp_path / "install"
    (install / "storage").mkdir(parents=True)
    (install / "astinuslab.toml").write_text("[app]\n", encoding="utf-8")
    victim = tmp_path / "outside"
    victim.mkdir()
    victim.chmod(0o777)
    (install / "storage/dumps").symlink_to(victim, target_is_directory=True)
    helper_dir = tmp_path / "helpers"
    helper_dir.mkdir()
    fake_chown = helper_dir / "chown"
    fake_chown.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_chown.chmod(0o755)
    harness = f"""
    source {INSTALL}
    INSTALL_DIR={install}
    API_USER=$(id -un)
    RUNNER_USER=$(id -un)
    ANALYSIS_USER=$(id -un)
    configure_split_permissions
    """

    result = run_bash(harness, env={"PATH": f"{helper_dir}:{os.environ['PATH']}"})

    assert result.returncode != 0
    assert victim.stat().st_mode & 0o777 == 0o777


def test_prepare_storage_rejects_symlinked_mutable_directory(tmp_path):
    install = tmp_path / "install"
    (install / "storage").mkdir(parents=True)
    victim = tmp_path / "outside"
    victim.mkdir()
    victim.chmod(0o777)
    (install / "storage/dumps").symlink_to(victim, target_is_directory=True)
    helper_dir = tmp_path / "helpers"
    helper_dir.mkdir()
    fake_chown = helper_dir / "chown"
    fake_chown.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_chown.chmod(0o755)
    harness = f"""
    source {INSTALL}
    INSTALL_DIR={install}
    SERVICE_USER=$(id -un)
    API_USER=$(id -un)
    prepare_storage
    """

    result = run_bash(harness, env={"PATH": f"{helper_dir}:{os.environ['PATH']}"})

    assert result.returncode != 0
    assert victim.stat().st_mode & 0o777 == 0o777


def test_secure_mutable_tree_rejects_fifo_without_blocking(tmp_path):
    mutable = tmp_path / "config"
    mutable.mkdir()
    os.mkfifo(mutable / "service-planted-fifo")
    harness = f"""
    source {INSTALL}
    secure_tree_permissions {mutable} $(id -un) astinuslab 0750 0640
    """

    result = subprocess.run(
        ["timeout", "2", "bash", "-c", harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.returncode != 124, "mutable-tree validation blocked while opening a FIFO"


def test_split_candidate_validation_rejects_fifo_without_blocking(tmp_path):
    install = tmp_path / "install"
    (install / "src/astinuslab").mkdir(parents=True)
    (install / "src/astinuslab/main.py").write_text("# candidate\n", encoding="utf-8")
    (install / ".venv/bin").mkdir(parents=True)
    candidate_python = install / ".venv/bin/python"
    candidate_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    candidate_python.chmod(0o755)
    (install / "storage").mkdir()
    (install / "storage/astinuslab.db").touch()
    (install / "storage/celery_broker.db").touch()
    os.mkfifo(install / "storage/celery_results.db")
    harness = f"""
    source {INSTALL}
    INSTALL_DIR={shlex.quote(str(install))}
    RELEASE_DIR={shlex.quote(str(install))}
    validate_split_candidate
    """

    result = subprocess.run(
        ["timeout", "2", "bash", "-c", harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.returncode != 124, "split candidate validation blocked while opening a FIFO"


def test_split_systemd_units_have_separate_identities_and_shared_writer_umask(tmp_path):
    install = tmp_path / "install"
    unit_dir = tmp_path / "units"
    release = install / "releases/1.0.0-test00000000"
    (release / "ops/systemd").mkdir(parents=True)
    for unit in ("astinuslab-api", "astinuslab-runner", "astinuslab-analysis"):
        shutil.copy(ROOT / "ops/systemd" / f"{unit}.service", release / "ops/systemd" / f"{unit}.service")
    unit_dir.mkdir()
    harness = f"""
    source {INSTALL}
    INSTALL_DIR={shlex.quote(str(install))}
    RELEASE_DIR={shlex.quote(str(release))}
    STORAGE_ROOT={shlex.quote(str(install / "storage"))}
    SYSTEMD_UNIT_DIR={shlex.quote(str(unit_dir))}
    SPLIT_USERS=true
    API_USER=astinuslab-api
    RUNNER_USER=astinuslab-runner
    ANALYSIS_USER=astinuslab-analysis
    systemctl() {{ :; }}
    install_systemd_units
    """
    result = run_bash(harness)
    assert result.returncode == 0, result.stderr

    api = (unit_dir / "astinuslab-api.service").read_text(encoding="utf-8")
    runner = (unit_dir / "astinuslab-runner.service").read_text(encoding="utf-8")
    analysis = (unit_dir / "astinuslab-analysis.service").read_text(encoding="utf-8")
    assert "User=astinuslab-api" in api and "Group=astinuslab" in api
    assert "User=astinuslab-runner" in runner and "Group=astinuslab" in runner
    assert "User=astinuslab-analysis" in analysis and "Group=astinuslab" in analysis
    assert "@%i.service" not in runner + analysis
    assert "NoNewPrivileges=true" in api
    assert "NoNewPrivileges=true" not in runner
    assert "PrivateDevices=true" not in runner
    assert "NoNewPrivileges=true" in analysis
    assert "PrivateDevices=true" in analysis
    assert "libvirtd.service" not in analysis
    for rendered in (api, runner, analysis):
        assert "UMask=0007" in rendered


def _render_units(tmp_path, *, split: bool, storage_root=None, install_dir=None):
    install = Path(install_dir) if install_dir is not None else tmp_path / "install"
    unit_dir = tmp_path / "units"
    release = (tmp_path / "release") if install_dir is not None else install / "releases/1.0.0-test00000000"
    (release / "ops/systemd").mkdir(parents=True)
    for unit in ("astinuslab-api", "astinuslab-runner", "astinuslab-analysis"):
        shutil.copy(ROOT / "ops/systemd" / f"{unit}.service", release / "ops/systemd" / f"{unit}.service")
    unit_dir.mkdir()
    storage = storage_root if storage_root is not None else install / "storage"
    identities = (
        """
    SPLIT_USERS=true
    API_USER=astinuslab-api
    RUNNER_USER=astinuslab-runner
    ANALYSIS_USER=astinuslab-analysis
    """
        if split
        else """
    SPLIT_USERS=false
    SERVICE_USER=operator
    API_USER=operator
    RUNNER_USER=operator
    ANALYSIS_USER=operator
    """
    )
    harness = f"""
    source {INSTALL}
    INSTALL_DIR={shlex.quote(str(install))}
    RELEASE_DIR={shlex.quote(str(release))}
    STORAGE_ROOT={shlex.quote(str(storage))}
    SYSTEMD_UNIT_DIR={shlex.quote(str(unit_dir))}
    {identities}
    systemctl() {{ :; }}
    install_systemd_units
    """
    result = run_bash(harness)
    assert result.returncode == 0, result.stderr
    suffix = "" if split else "@"
    return (
        install,
        storage,
        {
            name: (unit_dir / f"astinuslab-{name}{suffix}.service").read_text(encoding="utf-8")
            for name in ("api", "runner", "analysis")
        },
    )


@pytest.mark.parametrize("split", [True, False])
def test_rendered_read_write_paths_stay_scoped_per_service(tmp_path, split):
    install, storage, units = _render_units(tmp_path, split=split)
    expected = {
        "api": [str(storage), f"{install}/config", f"{install}/images/definitions", f"{install}/vol3/symbols"],
        "runner": [
            str(storage),
            f"{install}/images/definitions",
            f"{install}/vol3/symbols",
            "/var/lib/libvirt/images",
        ],
        "analysis": [str(storage), f"{install}/vol3/symbols"],
    }
    for name, paths in expected.items():
        assert "ProtectSystem=strict" in units[name]
        lines = [line for line in units[name].splitlines() if line.startswith("ReadWritePaths=")]
        assert len(lines) == 1, f"{name}: {lines}"
        assert lines[0].split("=", 1)[1].split() == paths, name


@pytest.mark.parametrize("split", [True, False])
def test_custom_evidence_root_keeps_operational_storage_writable(tmp_path, split):
    """`storage_dir` moves evidence, not SQLite and other operational state."""
    custom = Path("/srv/astinuslab-evidence")
    install, _, units = _render_units(tmp_path, split=split, storage_root=custom)

    for name, rendered in units.items():
        line = next(line for line in rendered.splitlines() if line.startswith("ReadWritePaths="))
        writable = line.split("=", 1)[1].split()
        assert str(custom) in writable, name
        assert str(install / "storage") in writable, name


@pytest.mark.parametrize("split", [True, False])
def test_rendered_units_leave_no_unsubstituted_placeholder(tmp_path, split):
    _, _, units = _render_units(tmp_path, split=split)
    for name, rendered in units.items():
        assert "@@" not in rendered, f"{name} kept an unsubstituted placeholder"
        assert "ProtectHome=" in rendered, name


def test_rendered_protect_home_follows_the_storage_root(tmp_path):
    _, _, under_opt = _render_units(tmp_path / "a", split=True)
    _, _, under_home = _render_units(tmp_path / "b", split=True, storage_root=Path("/home/operator/astinuslab"))

    for rendered in under_opt.values():
        assert "ProtectHome=read-only" in rendered
    for rendered in under_home.values():
        assert "ProtectHome=tmpfs" in rendered


def test_a_home_storage_root_is_bound_over_the_tmpfs_not_listed_read_write(tmp_path):
    """`ProtectHome=tmpfs` covers /home with an empty filesystem, and systemd
    cannot then bind a `ReadWritePaths=` entry that the tmpfs hides: the unit
    fails outright with `Failed to set up mount namespacing ... No such file or
    directory` (systemd 255, exit 226/NAMESPACE). `BindPaths=` mounts the real
    directory over the tmpfs and stays writable under `ProtectSystem=strict`,
    which keeps every other home hidden and the storage root reachable.
    """
    storage = Path("/home/operator/astinuslab")
    _, _, units = _render_units(tmp_path, split=False, storage_root=storage)

    for name, rendered in units.items():
        assert "ProtectHome=tmpfs" in rendered, name
        assert f"BindPaths={storage}" in rendered, name
        assert f"ReadWritePaths={storage}" not in rendered, name
        assert f"ReadWritePaths={storage} " not in rendered, name


def test_a_storage_root_outside_home_stays_a_plain_read_write_path(tmp_path):
    storage = Path("/srv/astinuslab-storage")
    _, _, units = _render_units(tmp_path, split=False, storage_root=storage)

    for name, rendered in units.items():
        assert "ProtectHome=read-only" in rendered, name
        assert f"ReadWritePaths={storage}" in rendered, name
        assert "BindPaths=" not in rendered, name


def test_an_install_root_under_home_keeps_home_readable_rather_than_hidden(tmp_path):
    """With the install root itself under /home, `ProtectHome=tmpfs` hides the
    source, the venv, config, images and the symbol cache all at once, and
    systemd fails the namespace on the first of them (226/NAMESPACE). Binding
    each one back is five mounts to keep in step with the unit; `read-only`
    leaves the whole tree reachable and the writable exceptions are the
    ReadWritePaths the unit already declares."""
    _, _, units = _render_units(tmp_path, split=False, install_dir=Path("/home/operator/astinuslab"))

    for name, rendered in units.items():
        assert "ProtectHome=read-only" in rendered, name
        assert "ProtectHome=tmpfs" not in rendered, name
        assert "BindPaths=" not in rendered, name


def test_configure_mac_grants_traversal_to_the_configured_storage_root(tmp_path):
    """A registered custom root only works if qemu can reach *it*. Granting the
    default tree instead leaves every capture under the real root failing, with
    nothing in the output to say why."""
    storage = tmp_path / "custom-root"
    storage.mkdir()
    template = tmp_path / "TEMPLATE.qemu"
    template.write_text("profile LIBVIRT_TEMPLATE {\n  #include <abstractions/libvirt-qemu>\n}\n", encoding="utf-8")
    template.chmod(0o644)

    result = run_root_mapped_bash(
        f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        STORAGE_ROOT={shlex.quote(str(storage))}
        grant_qemu_traversal() {{ echo "granted=$1"; }}
        systemctl() {{ :; }}
        configure_mac
        """,
        env={
            "ASTINUSLAB_APPARMOR_TEMPLATE": str(template),
            "ASTINUSLAB_MAC_POLICY_MANIFEST": str(tmp_path / "mac-policy.manifest"),
        },
    )

    assert f"granted={storage}" in result.stdout, result.stdout + result.stderr
    assert f'"{storage}/**" rwk,' in template.read_text(encoding="utf-8"), "AppArmor must permit the same root"


def test_configure_mac_surfaces_traversal_failure_when_called_conditionally(tmp_path):
    storage = tmp_path / "custom-root"
    storage.mkdir()

    result = run_bash(f"""
        source {INSTALL}
        STORAGE_ROOT={shlex.quote(str(storage))}
        ASTINUSLAB_APPARMOR_TEMPLATE={shlex.quote(str(tmp_path / "missing-template"))}
        grant_qemu_traversal() {{ return 42; }}
        if configure_mac; then
            echo unexpected-success
            exit 99
        fi
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "unexpected-success" not in result.stdout
    assert "MAC permissions configured" not in result.stdout


def test_configure_mac_fails_when_apparmor_cannot_reload(tmp_path):
    storage = tmp_path / "custom-root"
    storage.mkdir()
    template = tmp_path / "TEMPLATE.qemu"
    template.write_text("profile LIBVIRT_TEMPLATE {\n}\n", encoding="utf-8")
    template.chmod(0o644)

    result = run_root_mapped_bash(
        f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        STORAGE_ROOT={shlex.quote(str(storage))}
        grant_qemu_traversal() {{ :; }}
        systemctl() {{ [[ "$*" != "restart apparmor" ]]; }}
        configure_mac
        """,
        env={
            "ASTINUSLAB_APPARMOR_TEMPLATE": str(template),
            "ASTINUSLAB_MAC_POLICY_MANIFEST": str(tmp_path / "mac-policy.manifest"),
        },
    )

    assert result.returncode != 0, result.stdout + result.stderr


def test_configure_mac_rejects_an_apparmor_template_without_a_plain_closing_brace(tmp_path):
    storage = tmp_path / "custom-root"
    storage.mkdir()
    template = tmp_path / "TEMPLATE.qemu"
    template.write_text("profile LIBVIRT_TEMPLATE {\n  }\n", encoding="utf-8")
    template.chmod(0o644)

    result = run_root_mapped_bash(
        f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        STORAGE_ROOT={shlex.quote(str(storage))}
        grant_qemu_traversal() {{ :; }}
        systemctl() {{ :; }}
        configure_mac
        """,
        env={
            "ASTINUSLAB_APPARMOR_TEMPLATE": str(template),
            "ASTINUSLAB_MAC_POLICY_MANIFEST": str(tmp_path / "mac-policy.manifest"),
        },
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert f'"{storage}/**" rwk,' not in template.read_text(encoding="utf-8")


def test_configure_mac_fails_closed_when_selinux_tools_are_missing(tmp_path):
    storage = tmp_path / "custom-root"
    storage.mkdir()

    result = run_bash(
        f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        STORAGE_ROOT={shlex.quote(str(storage))}
        command() {{
          if [[ "$1" == -v && "$2" == semanage ]]; then return 1; fi
          if [[ "$1" == -v ]]; then return 0; fi
          builtin command "$@"
        }}
        getenforce() {{ echo Enforcing; }}
        grant_qemu_traversal() {{ :; }}
        configure_mac
        """,
        env={"ASTINUSLAB_APPARMOR_TEMPLATE": str(tmp_path / "missing-template")},
    )

    assert result.returncode != 0, result.stdout + result.stderr


def test_configure_mac_rejects_existing_selinux_rule_with_wrong_type(tmp_path):
    storage = tmp_path / "custom-root"
    storage.mkdir()
    expression = re.escape(str(storage)) + r"(/.*)?"

    result = run_bash(
        f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        STORAGE_ROOT={shlex.quote(str(storage))}
        command() {{
          if [[ "$1" == -v ]]; then return 0; fi
          builtin command "$@"
        }}
        getenforce() {{ echo Enforcing; }}
        semanage() {{ printf '%s all files system_u:object_r:default_t:s0\n' {shlex.quote(expression)}; }}
        restorecon() {{ :; }}
        grant_qemu_traversal() {{ :; }}
        configure_mac
        """,
        env={"ASTINUSLAB_APPARMOR_TEMPLATE": str(tmp_path / "missing-template")},
    )

    assert result.returncode != 0, result.stdout + result.stderr


def test_split_migration_verifies_new_helpers_before_old_stop_and_deprivilege():
    harness = f"""
    source {INSTALL}
    SPLIT_USERS=true
    SERVICE_USER=operator
    START_SERVICES=true
    ENABLE_SERVICES=true
    migration_snapshot() {{ echo snapshot; MIGRATION_SNAPSHOT=/tmp/fake-snapshot; }}
    migration_snapshot_mutable_state() {{ echo mutable-snapshot; }}
    ensure_split_storage_root() {{ :; }}
    bind_storage_root_identity() {{ :; }}
    assert_storage_root_identity() {{ :; }}
    install_transitional_sudoers() {{ echo transitional-sudoers; }}
    verify_runner_helpers() {{ echo verify-runner; }}
    stop_old_services() {{ echo stop-old; }}
    configure_split_permissions() {{ echo ownership; }}
    split_database_layout() {{ echo databases; }}
    remove_legacy_evidence_acls() {{ echo legacy-acls; }}
    install_systemd_units() {{ echo units; }}
    control_services() {{ echo control-new; }}
    verify_split_services() {{ echo verify-services; }}
    finalize_split_sudoers() {{ echo final-sudoers; }}
    verify_final_runner_helpers() {{ echo verify-final-runner; }}
    disable_old_services() {{ echo disable-old; }}
    migration_commit() {{ echo commit; }}
    cleanup_superseded_legacy_install() {{ :; }}
    migrate_to_split_users
    """
    result = run_bash(harness)
    assert result.returncode == 0, result.stderr
    events = [
        line
        for line in result.stdout.splitlines()
        if line
        in {
            "snapshot",
            "transitional-sudoers",
            "verify-runner",
            "stop-old",
            "ownership",
            "databases",
            "legacy-acls",
            "units",
            "control-new",
            "verify-services",
            "final-sudoers",
            "verify-final-runner",
            "disable-old",
            "commit",
        }
    ]
    assert events == [
        "snapshot",
        "transitional-sudoers",
        "verify-runner",
        "stop-old",
        "ownership",
        "databases",
        "legacy-acls",
        "units",
        "control-new",
        "verify-services",
        "final-sudoers",
        "verify-final-runner",
        "disable-old",
        "commit",
    ]


@pytest.mark.parametrize("wrapper", ["install_sudoers_rule", "install_transitional_sudoers", "finalize_split_sudoers"])
def test_sudoers_install_wrappers_propagate_helper_failure_in_conditional_context(tmp_path, wrapper):
    helper = tmp_path / "ops/host/install_sudoers_astinuslab.sh"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/bash\necho helper-publication-failed >&2\nexit 37\n", encoding="utf-8")
    helper.chmod(0o755)
    result = run_bash(f"""
        source {INSTALL}
        REPO_DIR={shlex.quote(str(tmp_path))}
        RELEASE_DIR={shlex.quote(str(tmp_path))}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        STORAGE_ROOT={shlex.quote(str(tmp_path / "storage"))}
        RUNNER_USER=astinuslab-runner
        SERVICE_USER=operator
        if {wrapper}; then exit 99; else status=$?; fi
        exit "$status"
    """)
    assert result.returncode == 37, result.stdout + result.stderr
    assert "helper-publication-failed" in result.stderr
    assert "Sudoers rule installed" not in result.stdout


def test_single_upgrade_rolls_back_when_sudoers_helper_fails(tmp_path):
    helper = tmp_path / "release/ops/host/install_sudoers_astinuslab.sh"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/bash\necho helper-publication-failed >&2\nexit 37\n", encoding="utf-8")
    helper.chmod(0o755)
    result = run_bash(f"""
        source {INSTALL}
        RELEASE_DIR={shlex.quote(str(tmp_path / "release"))}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        STORAGE_ROOT={shlex.quote(str(tmp_path / "storage"))}
        migration_snapshot() {{ :; }}
        install_source() {{ :; }}
        quiesce_services() {{ :; }}
        migration_snapshot_mutable_state() {{ :; }}
        prepare_single_upgrade_mutable_state() {{ :; }}
        prepare_storage() {{ :; }}
        backup_database_before_migration() {{ :; }}
        apply_db_migrations() {{ :; }}
        handle_ssh_key() {{ :; }}
        resolve_storage_root() {{ :; }}
        bind_storage_root_identity() {{ :; }}
        assert_storage_root_identity() {{ :; }}
        configure_mac() {{ echo UNEXPECTED-PUBLICATION; }}
        install_systemd_units() {{ echo UNEXPECTED-PUBLICATION; }}
        switch_release_and_restart() {{ echo UNEXPECTED-PUBLICATION; }}
        verify_release_readiness() {{ :; }}
        migration_discard_snapshot() {{ echo UNEXPECTED-COMMIT; }}
        cleanup_superseded_legacy_install() {{ :; }}
        release_rollback() {{ echo RELEASE-ROLLBACK; }}
        migration_rollback() {{ echo PRIVILEGED-ROLLBACK; }}
        if upgrade_single_identity_install; then exit 99; else exit "$?"; fi
    """)
    assert result.returncode == 1, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "helper-publication-failed" in output
    assert "Sudoers installation failed" in output
    assert "RELEASE-ROLLBACK" in output and "PRIVILEGED-ROLLBACK" in output
    assert "UNEXPECTED-" not in output
    assert "Sudoers rule installed" not in output


def test_split_helper_policy_and_verification_execute_every_safe_probe(tmp_path):
    helper_source = tmp_path / "ops/host"
    helper_source.mkdir(parents=True)
    installer = helper_source / "install_sudoers_astinuslab.sh"
    installer.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*"\n', encoding="utf-8")
    installer.chmod(0o755)
    runuser_log = tmp_path / "runuser.log"
    harness = f"""
    source {INSTALL}
    REPO_DIR={tmp_path}
    INSTALL_DIR=/opt/astinuslab
    STORAGE_ROOT=/opt/astinuslab/storage
    RUNNER_USER=astinuslab-runner
    SERVICE_USER=operator
    runuser() {{ printf 'runuser %s\\n' "$*" >> {runuser_log}; }}
    install_transitional_sudoers
    verify_runner_helpers
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr
    assert "--evidence-read-group astinuslab" in result.stdout
    probes = runuser_log.read_text(encoding="utf-8")
    assert "fix_libvirt_path_acl.sh --probe" in probes
    assert "libvirt_storage_admin.sh noop" in probes
    assert "network_isolation_admin.sh noop" in probes


def test_split_rerun_stops_active_explicit_units_before_payload_update():
    harness = f"""
    source {INSTALL}
    SERVICE_USER=operator
    declare -A stopped=()
    systemctl() {{
      case "$1" in
        show)
          if [[ "$4" == ActiveState ]]; then
            [[ -n "${{stopped[$2]:-}}" ]] && echo inactive || echo active
          else
            echo 0
          fi
          ;;
        stop) stopped[$2]=true; echo systemctl "$@" ;;
        *) return 1 ;;
      esac
    }}
    stop_old_services
    """
    result = run_bash(harness)
    assert result.returncode == 0, result.stderr
    stopped = [line for line in result.stdout.splitlines() if line.startswith("systemctl stop")]
    assert stopped == [
        "systemctl stop astinuslab-api@operator.service",
        "systemctl stop astinuslab-runner@operator.service",
        "systemctl stop astinuslab-analysis@operator.service",
        "systemctl stop astinuslab-api.service",
        "systemctl stop astinuslab-runner.service",
        "systemctl stop astinuslab-analysis.service",
    ]


def test_split_migration_failure_restores_privileged_files_ownership_and_units():
    harness = f"""
    source {INSTALL}
    SPLIT_USERS=true
    SERVICE_USER=operator
    migration_snapshot() {{ echo snapshot; MIGRATION_SNAPSHOT=/tmp/fake-snapshot; }}
    ensure_split_storage_root() {{ :; }}
    install_transitional_sudoers() {{ echo transitional-sudoers; }}
    verify_runner_helpers() {{ echo verify-runner; return 1; }}
    migration_rollback() {{ echo rollback-sudoers-helpers-ownership-units; }}
    if migrate_to_split_users; then exit 99; fi
    """
    result = run_bash(harness)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "rollback-sudoers-helpers-ownership-units"


def test_split_migration_commit_failure_also_rolls_back():
    harness = f"""
    source {INSTALL}
    SPLIT_USERS=true
    SERVICE_USER=operator
    migration_snapshot() {{ MIGRATION_SNAPSHOT=/tmp/fake-snapshot; }}
    migration_snapshot_mutable_state() {{ :; }}
    ensure_split_storage_root() {{ :; }}
    bind_storage_root_identity() {{ :; }}
    assert_storage_root_identity() {{ :; }}
    install_transitional_sudoers() {{ :; }}
    verify_runner_helpers() {{ :; }}
    stop_old_services() {{ :; }}
    configure_split_permissions() {{ :; }}
    split_database_layout() {{ :; }}
    remove_legacy_evidence_acls() {{ :; }}
    install_systemd_units() {{ :; }}
    control_services() {{ :; }}
    verify_split_services() {{ :; }}
    finalize_split_sudoers() {{ :; }}
    verify_final_runner_helpers() {{ :; }}
    disable_old_services() {{ :; }}
    migration_commit() {{ echo commit-failed; return 1; }}
    cleanup_superseded_legacy_install() {{ :; }}
    migration_rollback() {{ echo rollback-after-commit-failure; }}
    if migrate_to_split_users; then exit 99; fi
    """
    result = run_bash(harness)
    assert result.returncode == 0, result.stderr
    assert "commit-failed" in result.stdout
    assert result.stdout.splitlines()[-1] == "rollback-after-commit-failure"


def test_split_identity_transaction_is_content_neutral():
    harness = f"""
    source {INSTALL}
    SPLIT_USERS=true
    SERVICE_USER=operator
    migration_snapshot() {{ MIGRATION_SNAPSHOT=/tmp/fake-snapshot; }}
    migration_snapshot_mutable_state() {{ :; }}
    ensure_split_storage_root() {{ :; }}
    bind_storage_root_identity() {{ :; }}
    assert_storage_root_identity() {{ :; }}
    install_transitional_sudoers() {{ :; }}
    verify_runner_helpers() {{ :; }}
    stop_old_services() {{ :; }}
    install_source() {{ echo FORBIDDEN-source; }}
    create_venv() {{ echo FORBIDDEN-venv; }}
    apply_db_migrations() {{ echo FORBIDDEN-migration; }}
    configure_mac() {{ echo FORBIDDEN-mac; }}
    configure_split_permissions() {{ :; }}
    split_database_layout() {{ :; }}
    remove_legacy_evidence_acls() {{ :; }}
    install_systemd_units() {{ :; }}
    control_services() {{ :; }}
    verify_split_services() {{ :; }}
    finalize_split_sudoers() {{ :; }}
    verify_final_runner_helpers() {{ :; }}
    disable_old_services() {{ :; }}
    migration_commit() {{ :; }}
    cleanup_superseded_legacy_install() {{ :; }}
    migrate_to_split_users
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr
    assert "FORBIDDEN" not in result.stdout


def test_migration_rollback_propagates_restore_failure(tmp_path):
    snapshot = tmp_path / "snapshot"
    (snapshot / "units").mkdir(parents=True)
    (snapshot / "sudoers").write_text("rule\n", encoding="utf-8")
    (snapshot / "users.missing").write_text(pwd.getpwuid(os.getuid()).pw_name + "\n")
    install = tmp_path / "install"
    install.mkdir()
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    harness = f"""
    source {INSTALL}
    MIGRATION_SNAPSHOT={snapshot}
    INSTALL_DIR={install}
    SYSTEMD_UNIT_DIR={unit_dir}
    SUDOERS_FILE={tmp_path / "sudoers"}
    HELPER_INSTALL_DIR={tmp_path / "libexec"}
    POLICY_FILE={tmp_path / "policy"}
    RUNTIME_IDENTITIES_FILE={tmp_path / "identities"}
    cp() {{ return 1; }}
    userdel() {{ echo forbidden-identity-delete; }}
    systemctl() {{ echo "$*" >> {tmp_path / "systemctl.log"}; }}
    install() {{ :; }}
    visudo() {{ :; }}
    setfacl() {{ :; }}
    if migration_rollback; then exit 99; fi
    echo rollback-failure-propagated
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr
    assert "rollback-failure-propagated" in result.stdout
    assert "forbidden-identity-delete" not in result.stdout
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8")
    assert "start " not in calls, "partial privileged restore must leave every writer stopped"


def test_migration_rollback_quiesced_mode_restores_neither_enablement_nor_activity(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "enabled").write_text("astinuslab-api@operator.service\n", encoding="utf-8")
    (snapshot / "active").write_text("astinuslab-api@operator.service\n", encoding="utf-8")
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    install = tmp_path / "install"
    install.mkdir()
    call_log = tmp_path / "systemctl.log"

    result = run_bash(f"""
    source {INSTALL}
    MIGRATION_SNAPSHOT={shlex.quote(str(snapshot))}
    INSTALL_DIR={shlex.quote(str(install))}
    STORAGE_ROOT={shlex.quote(str(install / "storage"))}
    SYSTEMD_UNIT_DIR={shlex.quote(str(unit_dir))}
    SUDOERS_FILE={shlex.quote(str(tmp_path / "sudoers"))}
    HELPER_INSTALL_DIR={shlex.quote(str(tmp_path / "libexec"))}
    POLICY_FILE={shlex.quote(str(tmp_path / "policy"))}
    RUNTIME_IDENTITIES_FILE={shlex.quote(str(tmp_path / "identities"))}
    SERVICE_USER=operator
    quiesce_services() {{ :; }}
    rollback_snapshot_host_access() {{ :; }}
    undo_migration_created_dirs() {{ :; }}
    visudo() {{ :; }}
    systemctl() {{ echo "$*" >> {shlex.quote(str(call_log))}; return 0; }}
    migration_rollback false
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("enable ") or line.startswith("start ") for line in calls)


@pytest.mark.parametrize("failure", ["stop", "disable"])
def test_migration_rollback_aborts_before_restore_when_service_custody_fails(tmp_path, failure):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    harness = f"""
    source {INSTALL}
    MIGRATION_SNAPSHOT={snapshot}
    SERVICE_USER=$(id -un)
    systemctl() {{
      case "$1" in
        show)
          if [[ "$4" == ActiveState ]]; then
            [[ {failure} == stop ]] && echo active || echo inactive
          else
            echo 0
          fi
          ;;
        stop) echo stop-attempted; return 1 ;;
        is-enabled) [[ {failure} == disable ]] && return 0 || return 1 ;;
        disable) echo disable-attempted; return 1 ;;
        *) echo FORBIDDEN-systemctl-$1; return 1 ;;
      esac
    }}
    rollback_snapshot_host_access() {{ echo FORBIDDEN-host-restore; }}
    restore_protected_regular() {{ echo FORBIDDEN-file-restore; }}
    undo_migration_created_dirs() {{ echo FORBIDDEN-dir-restore; }}
    userdel() {{ echo FORBIDDEN-user-delete; }}
    if migration_rollback; then exit 99; fi
    echo rollback-refused
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{failure}-attempted" in result.stdout
    assert "rollback-refused" in result.stdout
    assert "FORBIDDEN" not in result.stdout
    assert "refusing to restore privileged or identity state" in result.stdout + result.stderr


def test_managed_directory_records_only_the_directories_it_creates(tmp_path):
    root = tmp_path / "storage"
    (root / "playground").mkdir(parents=True)
    record = tmp_path / "created-dirs"
    harness = f"""
    source {INSTALL}
    MIGRATION_CREATED_DIRS={shlex.quote(str(record))}
    prepare_managed_directory playground/disks $(id -un) $(id -gn) 0700 {shlex.quote(str(root))}
    prepare_managed_directory playground $(id -un) $(id -gn) 0700 {shlex.quote(str(root))}
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr
    assert record.read_text(encoding="utf-8").split() == [str(root / "playground/disks")]


def test_migration_rollback_undoes_directories_the_migration_created(tmp_path):
    snapshot = tmp_path / "snapshot"
    (snapshot / "units").mkdir(parents=True)
    (snapshot / "traversal-acl.before").write_text("", encoding="utf-8")
    (snapshot / "mac-policy.before").write_text("", encoding="utf-8")
    install = tmp_path / "install"
    storage = install / "storage"
    (storage / "playground/disks").mkdir(parents=True)
    (storage / "workload-cache/staging").mkdir(parents=True)
    kept = storage / "playground/captures"
    kept.mkdir()
    (kept / "evidence.bin").write_text("capture\n", encoding="utf-8")
    (snapshot / "created-dirs").write_text(
        "\n".join(
            (
                str(storage / "playground"),
                str(storage / "playground/disks"),
                str(kept),
                str(storage / "workload-cache"),
                str(storage / "workload-cache/staging"),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    harness = f"""
    source {INSTALL}
    MIGRATION_SNAPSHOT={shlex.quote(str(snapshot))}
    INSTALL_DIR={shlex.quote(str(install))}
    RELEASE_DIR={shlex.quote(str(install))}
    STORAGE_ROOT={shlex.quote(str(storage))}
    SERVICE_USER=$(id -un)
    SYSTEMD_UNIT_DIR={unit_dir}
    SUDOERS_FILE={tmp_path / "sudoers"}
    HELPER_INSTALL_DIR={tmp_path / "libexec"}
    POLICY_FILE={tmp_path / "policy"}
    RUNTIME_IDENTITIES_FILE={tmp_path / "identities"}
    systemctl() {{
      case "$1" in
        show) [[ "$4" == ActiveState ]] && echo inactive || echo 0 ;;
        is-enabled) return 1 ;;
        *) return 0 ;;
      esac
    }}
    visudo() {{ :; }}
    migration_rollback
    echo "rollback-exit=$?"
    """

    result = run_bash(harness)

    assert "rollback-exit=0" in result.stdout, result.stdout + result.stderr
    assert not (storage / "playground/disks").exists(), "an empty created directory must be removed"
    assert not (storage / "workload-cache/staging").exists(), "a created leaf must be removed"
    assert not (storage / "workload-cache").exists(), "a created parent emptied by the undo must be removed too"
    assert (kept / "evidence.bin").read_text(encoding="utf-8") == "capture\n", "rollback must never destroy bytes"
    assert (storage / "playground").is_dir(), "a created parent still holding evidence must survive"


def test_acl_snapshot_restore_tolerates_only_missing_sqlite_sidecars(tmp_path):
    database_root = tmp_path / "database"
    material_root = tmp_path / "material"
    database_root.mkdir()
    material_root.mkdir()
    existing = database_root / "astinuslab.db"
    sidecar = database_root / "astinuslab.db-wal"
    missing_material = material_root / "evidence.bin"
    existing.write_text("db\n", encoding="utf-8")
    sidecar.write_text("wal\n", encoding="utf-8")
    missing_material.write_text("evidence\n", encoding="utf-8")
    existing.chmod(0o640)
    snapshot = tmp_path / "snapshot.acl"
    material_snapshot = tmp_path / "material.acl"
    captured = run_bash(
        f"source {INSTALL}; snapshot_acl_state {shlex.quote(str(database_root))} "
        f"{shlex.quote(str(snapshot))}; snapshot_acl_state {shlex.quote(str(material_root))} "
        f"{shlex.quote(str(material_snapshot))}"
    )
    assert captured.returncode == 0, captured.stderr
    sidecar.unlink()
    missing_material.unlink()
    existing.chmod(0o600)

    database_dir = shlex.quote(str(database_root))
    restored = run_bash(f"source {INSTALL}; restore_acl_snapshot {snapshot} {database_dir}")
    refused = run_bash(f"source {INSTALL}; restore_acl_snapshot {material_snapshot} {shlex.quote(str(material_root))}")

    assert restored.returncode == 0, restored.stderr
    assert existing.stat().st_mode & 0o777 == 0o640
    assert refused.returncode != 0


def test_acl_snapshot_restore_rejects_recognized_sidecar_outside_database_directories(tmp_path):
    storage = tmp_path / "storage"
    evidence = tmp_path / "evidence"
    storage.mkdir()
    evidence.mkdir()
    decoy = evidence / "astinuslab.db-wal"
    decoy.write_text("operator evidence, not a database sidecar\n", encoding="utf-8")
    snapshot = tmp_path / "decoy.acl"
    captured = run_bash(
        f"source {INSTALL}; snapshot_acl_state {shlex.quote(str(evidence))} {shlex.quote(str(snapshot))}"
    )
    assert captured.returncode == 0, captured.stderr
    decoy.unlink()

    result = run_bash(
        f"source {INSTALL}; restore_acl_snapshot {shlex.quote(str(snapshot))} {shlex.quote(str(storage))}"
    )

    assert result.returncode != 0, result.stdout
    assert str(decoy) in result.stderr


def test_acl_snapshot_restore_tolerates_purged_playground_scratch(tmp_path):
    """Split services start before the migration commits, and their startup
    recovery purges playground scratch for sessions that no longer exist. That
    subtree is service-managed and reconstructible; captures are not."""
    scratch_storage = tmp_path / "scratch-storage"
    capture_storage = tmp_path / "capture-storage"
    tree_storage = tmp_path / "tree-storage"
    scratch = scratch_storage / "playground/disks/pg-001"
    captures = capture_storage / "playground/captures/pg-001"
    tree_scratch = tree_storage / "playground/disks/pg-001"
    scratch.mkdir(parents=True)
    captures.mkdir(parents=True)
    tree_scratch.mkdir(parents=True)
    (scratch / "seed.iso").write_text("seed\n", encoding="utf-8")
    (captures / "memory.elf").write_text("evidence\n", encoding="utf-8")
    (tree_scratch / "seed.iso").write_text("seed\n", encoding="utf-8")

    def snapshot_of(name, root):
        destination = tmp_path / name
        captured = run_bash(
            f"source {INSTALL}; snapshot_acl_state {shlex.quote(str(root))} {shlex.quote(str(destination))}"
        )
        assert captured.returncode == 0, captured.stderr
        return destination

    scratch_snapshot = snapshot_of("scratch.acl", scratch_storage)
    capture_snapshot = snapshot_of("capture.acl", capture_storage)
    tree_snapshot = snapshot_of("tree.acl", tree_storage)
    shutil.rmtree(scratch)
    shutil.rmtree(captures)

    purged = run_bash(f"source {INSTALL}; restore_acl_snapshot {scratch_snapshot} {shlex.quote(str(scratch_storage))}")
    evidence = run_bash(
        f"source {INSTALL}; restore_acl_snapshot {capture_snapshot} {shlex.quote(str(capture_storage))}"
    )

    shutil.rmtree(tree_storage / "playground/disks")
    missing_tree = run_bash(f"source {INSTALL}; restore_acl_snapshot {tree_snapshot} {shlex.quote(str(tree_storage))}")

    assert purged.returncode == 0, purged.stderr
    assert evidence.returncode != 0, "a vanished capture must still fail the rollback"
    assert str(captures) in evidence.stderr
    assert missing_tree.returncode != 0, "the scratch tree itself vanishing is material"


@pytest.mark.parametrize("swapped", ["file", "parent"])
def test_acl_restore_rejects_symlink_swaps_without_touching_outside(tmp_path, swapped):
    root = tmp_path / "root"
    tree = root / "tree"
    tree.mkdir(parents=True)
    recorded = tree / "recorded"
    recorded.write_text("recorded\n", encoding="utf-8")
    recorded.chmod(0o640)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "recorded"
    sentinel.write_text("outside\n", encoding="utf-8")
    sentinel.chmod(0o666)
    snapshot = tmp_path / "swap.acl"
    captured = run_bash(f"source {INSTALL}; snapshot_acl_state {shlex.quote(str(root))} {shlex.quote(str(snapshot))}")
    assert captured.returncode == 0, captured.stderr

    if swapped == "file":
        recorded.rename(tree / "recorded.saved")
        recorded.symlink_to(sentinel)
    else:
        tree.rename(root / "tree.saved")
        tree.symlink_to(outside, target_is_directory=True)

    result = run_bash(f"source {INSTALL}; restore_acl_snapshot {shlex.quote(str(snapshot))} {shlex.quote(str(root))}")

    assert result.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "outside\n"
    assert sentinel.stat().st_mode & 0o777 == 0o666


def test_migration_rollback_uses_disappearance_aware_acl_restore():
    """Both halves of the snapshot address entries by descriptor, never by path.

    The capture and replay bodies moved out of this script into
    `ops/deploy/acl_snapshot.py`, so the descriptor-addressing properties are
    asserted where the code now lives. What stays here is the wiring: the
    rollback path must still call the restore with the two snapshots it took,
    and it must pass the storage roots whose transient contents may legitimately
    have disappeared.
    """
    text = INSTALL.read_text(encoding="utf-8")
    module = ACL_SNAPSHOT.read_text(encoding="utf-8")
    capture = module[module.index("def _acl_text(") : module.index("def _load_snapshot(")]
    restore = module[module.index("def restore(") :]
    rollback = text[text.index("migration_rollback()") : text.index("migration_commit()")]

    assert rollback.count("restore_acl_snapshot") == 2
    assert "getfacl" in capture and "/proc/self/fd/{fd}" in capture
    assert "setfacl --restore=" not in text and "setfacl --restore=" not in module
    assert 'f"/proc/self/fd/{descriptor}"' in restore
    assert "os.O_NOFOLLOW" in module
    assert 'restore_acl_snapshot "$MIGRATION_SNAPSHOT/install.acl" "$INSTALL_DIR/storage"' in rollback
    assert 'restore_acl_snapshot "$MIGRATION_SNAPSHOT/storage-root.acl" "$STORAGE_ROOT"' in rollback


def test_the_installer_calls_the_extracted_acl_snapshot_module():
    """The shell entry points survive the extraction; only their bodies moved.

    `snapshot_acl_state` and `restore_acl_snapshot` are called from the
    migration paths and directly by this suite, so the argument order they
    forward is a contract, not an implementation detail.
    """
    text = INSTALL.read_text(encoding="utf-8")
    snapshot = text[text.index("snapshot_acl_state()") : text.index("migration_snapshot_mutable_state()")]
    restore = text[text.index("restore_acl_snapshot()") : text.index("undo_migration_created_dirs()")]

    assert 'python3 "$SCRIPT_DIR/acl_snapshot.py" capture "$root" "$destination"' in snapshot
    assert 'python3 "$SCRIPT_DIR/acl_snapshot.py" restore "$snapshot" "$@"' in restore
    assert "<<'PY'" not in snapshot and "<<'PY'" not in restore


def test_upgrade_snapshot_covers_privileged_access_manifests():
    text = INSTALL.read_text(encoding="utf-8")
    snapshot = text[text.index("migration_snapshot()") : text.index("restore_acl_snapshot()")]
    rollback = text[text.index("migration_rollback()") : text.index("migration_commit()")]

    assert "TRAVERSAL_ACL_MANIFEST" in snapshot
    assert "MAC_POLICY_MANIFEST" in snapshot
    assert "rollback_snapshot_host_access" in rollback


def test_split_service_verification_waits_and_runs_deep_readiness(tmp_path):
    install = tmp_path / "installed-candidate"
    (install / ".venv/bin").mkdir(parents=True)
    candidate_python = install / ".venv/bin/python"
    candidate_python.write_text(
        f"#!/usr/bin/env bash\n[[ $PWD == {shlex.quote(str(install))} ]] || exit 86\nexit 0\n",
        encoding="utf-8",
    )
    candidate_python.chmod(0o755)
    harness = f"""
    source {INSTALL}
    START_SERVICES=true
    API_USER=astinuslab-api
    RUNNER_USER=astinuslab-runner
    ANALYSIS_USER=astinuslab-analysis
    INSTALL_DIR={shlex.quote(str(install))}
    RELEASE_DIR={shlex.quote(str(install))}
    systemctl() {{ echo systemctl "$@"; return 0; }}
    sleep() {{ echo sleep "$@"; }}
    verify_runner_helpers() {{ echo verify-helpers; }}
    runuser() {{
      if [[ "$*" == *"sudo -n -l"* ]]; then return 1; fi
      if [[ "$*" == *"astinuslab.readiness"* ]]; then
        echo runuser "$@"
        while [[ "$1" != -- ]]; do shift; done
        shift
        "$@"
        return
      fi
      return 0
    }}
    id() {{
      if [[ "${{1:-}}" == -nG ]]; then echo astinuslab; return 0; fi
      return 0
    }}
    verify_split_services
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("systemctl is-active --quiet") >= 6
    assert "astinuslab.readiness" in result.stdout
    assert "mandatory_runtime_ready" in result.stdout
    assert "sleep" in result.stdout


@pytest.mark.parametrize("location", ["install/storage", "external-storage"])
@pytest.mark.parametrize("legacy_is_runner", [False, True])
def test_split_upgrade_preserves_migrated_evidence_acls(tmp_path, location, legacy_is_runner):
    storage = tmp_path / location
    capture = storage / "playground/captures/run-1/capture.bin"
    capture.parent.mkdir(parents=True)
    capture.write_bytes(b"retained capture")
    disk = storage / "playground/disks/private.qcow2"
    disk.parent.mkdir()
    disk.write_bytes(b"private disk")
    identity = shlex.quote(pwd.getpwuid(os.getuid()).pw_name)
    setup = f"""
    source {INSTALL}
    STORAGE_ROOT={shlex.quote(str(storage))}
    SERVICE_USER={identity if legacy_is_runner else "nobody"}
    API_USER={identity}
    RUNNER_USER={identity}
    ANALYSIS_USER={identity}
    # Use the real custody functions with a single test UID; no host mutation.
    secure_runtime_control_lock_permissions() {{ :; }}
    secure_package_storage_permissions() {{ :; }}
    provision_service_logs() {{ :; }}
    install_runtime_config_defaults() {{ :; }}
    configure_admin_auth() {{ :; }}
    split_database_layout() {{ :; }}
    configure_split_permissions() {{ configure_split_storage_permissions "$STORAGE_ROOT"; }}
    """
    # Establish the same evidence grants migration installs, including the
    # case where the selected service user is already the runner identity.
    result = run_bash(
        setup
        + f'configure_split_permissions && python3 {PERMISSIONS} drop-legacy-evidence-acl "$STORAGE_ROOT" "$SERVICE_USER"'
    )
    assert result.returncode == 0, result.stderr

    def snapshot():
        return {
            str(path.relative_to(storage)): (
                path.stat().st_mode,
                subprocess.check_output(["getfacl", "-cp", str(path)], text=True),
                path.read_bytes() if path.is_file() else None,
            )
            for path in (storage, *storage.rglob("*"))
        }

    before = snapshot()
    for _ in range(2):
        result = run_bash(setup + "prepare_split_upgrade_mutable_state")
        assert result.returncode == 0, result.stderr
        assert snapshot() == before, "An existing split upgrade must preserve effective evidence ACLs"
    acl = subprocess.check_output(["getfacl", "-cpE", str(storage / "playground")], text=True)
    assert "group:astinuslab:--x\nmask::--x" in acl
    assert disk.stat().st_mode & 0o777 == 0o600


def test_split_upgrade_aborts_when_evidence_acl_reconciliation_fails(tmp_path):
    database_marker = tmp_path / "database-started"
    result = run_bash(f"""
    source {INSTALL}
    install_runtime_config_defaults() {{ :; }}
    configure_admin_auth() {{ :; }}
    configure_split_permissions() {{ :; }}
    remove_legacy_evidence_acls() {{ return 42; }}
    split_database_layout() {{ touch {shlex.quote(str(database_marker))}; }}
    prepare_split_upgrade_mutable_state
    """)
    assert result.returncode != 0
    assert not database_marker.exists()


def test_split_reconciles_registered_external_storage_root(tmp_path):
    storage = tmp_path / "external-storage"
    storage.mkdir()
    (storage / "logs").mkdir()
    (storage / "logs/runner.log").write_text("legacy\n", encoding="utf-8")
    identity = os.environ.get("USER", "operator")
    harness = f"""
    source {INSTALL}
    # This fixture uses one UID; real distinct-UID ACL proof is in test_runtime_control_locks.py.
    secure_runtime_control_lock_permissions() {{ :; }}
    secure_package_storage_permissions() {{ :; }}
    provision_service_logs() {{ :; }}
    SERVICE_USER={shlex.quote(identity)}
    API_USER={shlex.quote(identity)}
    RUNNER_USER={shlex.quote(identity)}
    ANALYSIS_USER={shlex.quote(identity)}
    configure_split_storage_permissions {shlex.quote(str(storage))}
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr
    expected_modes = {
        "": 0o2770,
        "dumps": 0o2750,
        "tmp": 0o0750,
        "workload-cache": 0o0750,
        "playground": 0o0700,
        "playground/disks": 0o0700,
        "playground/captures": 0o0750,
    }
    for relative, mode in expected_modes.items():
        assert storage.joinpath(relative).stat().st_mode & 0o7777 == mode
    assert (storage / "logs/runner.log").read_text() == "legacy\n"
    for database in ("astinuslab.db", "celery_broker.db", "celery_results.db"):
        assert not storage.joinpath(database).exists()


@pytest.mark.parametrize("storage_relation", ["nested", "ancestor"])
def test_split_refuses_overlapping_storage_before_permission_mutation(tmp_path, storage_relation):
    allowed_base = ROOT / ".pytest-overlap" / tmp_path.name
    shutil.rmtree(allowed_base, ignore_errors=True)
    allowed_base.mkdir(parents=True)
    if storage_relation == "nested":
        install = allowed_base / "install"
        storage = install / "custom-storage"
    else:
        storage = allowed_base / "storage-ancestor"
        install = storage / "install"
    evidence = storage / "dumps/job-1/evidence.bin"
    evidence.parent.mkdir(parents=True)
    install.mkdir(parents=True, exist_ok=True)
    evidence.write_bytes(b"preserve-me")
    evidence.chmod(0o440)
    original = (evidence.read_bytes(), evidence.stat().st_uid, evidence.stat().st_gid, evidence.stat().st_mode)
    mutation_marker = tmp_path / "permission-command-ran"
    harness = f"""
    source {INSTALL}
    INSTALL_DIR={shlex.quote(str(install))}
    RELEASE_DIR={shlex.quote(str(install))}
    STORAGE_ROOT={shlex.quote(str(storage))}
    chown() {{ touch {shlex.quote(str(mutation_marker))}; command chown "$@"; }}
    chmod() {{ touch {shlex.quote(str(mutation_marker))}; command chmod "$@"; }}
    validate_install_storage_root
    """

    result = run_bash(harness)

    assert result.returncode != 0
    assert "path-disjoint" in result.stderr
    assert not mutation_marker.exists()
    assert (evidence.read_bytes(), evidence.stat().st_uid, evidence.stat().st_gid, evidence.stat().st_mode) == original
    shutil.rmtree(allowed_base)


def test_split_storage_topology_validation_precedes_candidate_bootstrap():
    text = INSTALL.read_text(encoding="utf-8")
    main = text[text.index("main() {") :]
    assert main.index("validate_install_storage_root") < main.index("bootstrap_split_candidate_if_missing")


@pytest.mark.parametrize("unsafe_kind", ["broad", "nested"])
def test_no_flag_install_refuses_unsafe_storage_before_root_or_mutation(tmp_path, unsafe_kind):
    allowed_base = ROOT / ".pytest-unsafe-storage" / tmp_path.name
    shutil.rmtree(allowed_base, ignore_errors=True)
    allowed_base.mkdir(parents=True)
    install = allowed_base / "install"
    install.mkdir()
    if unsafe_kind == "broad":
        storage = Path("/opt")
        expected = "not a registrable storage directory"
    else:
        storage = install / "custom-storage"
        storage.mkdir()
        evidence = storage / "evidence.bin"
        evidence.write_bytes(b"preserve")
        evidence.chmod(0o440)
        expected = "path-disjoint"
    unit_dir = tmp_path / "units"
    policy = tmp_path / "acl-policy.json"
    helper_dir = tmp_path / "libexec"
    result = subprocess.run(
        [
            "bash",
            str(INSTALL),
            "--install-dir",
            str(install),
            "--storage-root",
            str(storage),
            "--no-start",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "ASTINUSLAB_SYSTEMD_UNIT_DIR": str(unit_dir),
            "ASTINUSLAB_POLICY_FILE": str(policy),
            "ASTINUSLAB_HELPER_INSTALL_DIR": str(helper_dir),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert expected in result.stderr
    assert "Run this script as root" not in result.stderr
    assert not unit_dir.exists()
    assert not policy.exists()
    assert not helper_dir.exists()
    if unsafe_kind == "nested":
        assert evidence.read_bytes() == b"preserve"
        assert evidence.stat().st_mode & 0o777 == 0o440
    shutil.rmtree(allowed_base)


def test_storage_validation_precedes_no_flag_prerequisites():
    text = INSTALL.read_text(encoding="utf-8")
    main = text[text.index("main() {") :]
    assert main.index("validate_install_storage_root") < main.index("check_prerequisites")


@pytest.mark.parametrize("failed_probe", [1, 2])
def test_runner_helper_verification_propagates_early_probe_failure(failed_probe):
    harness = f"""
    source {INSTALL}
    RUNNER_USER=astinuslab-runner
    calls=0
    runuser() {{
      calls=$((calls + 1))
      [[ "$calls" != {failed_probe} ]]
    }}
    if verify_runner_helpers; then exit 99; fi
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr


def test_split_service_verification_propagates_deep_readiness_failure():
    harness = f"""
    source {INSTALL}
    START_SERVICES=true
    API_USER=astinuslab-api
    RUNNER_USER=astinuslab-runner
    ANALYSIS_USER=astinuslab-analysis
    INSTALL_DIR=/opt/astinuslab
    systemctl() {{ return 0; }}
    sleep() {{ :; }}
    verify_runner_helpers() {{ :; }}
    runuser() {{
      if [[ "$*" == *"astinuslab.readiness"* ]]; then return 42; fi
      return 1
    }}
    id() {{ return 1; }}
    if verify_split_services; then exit 99; fi
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr


def test_service_control_propagates_middle_unit_failure():
    harness = f"""
    source {INSTALL}
    SPLIT_USERS=true
    ENABLE_SERVICES=false
    START_SERVICES=true
    RESTART_SERVICES=false
    systemctl() {{ [[ "$2" != astinuslab-runner.service ]]; }}
    if control_services; then exit 99; fi
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr


def test_migration_commit_propagates_late_marker_permission_failure(tmp_path):
    snapshot = tmp_path / "astinuslab-split.snapshot"
    snapshot.mkdir()
    marker = tmp_path / "etc/runtime-identities"
    harness = f"""
    source {INSTALL}
    split_database_layout() {{ :; }}
    API_USER=astinuslab-api
    RUNNER_USER=astinuslab-runner
    ANALYSIS_USER=astinuslab-analysis
    RUNTIME_IDENTITIES_FILE={marker}
    MIGRATION_SNAPSHOT_PARENT={tmp_path}
    MIGRATION_SNAPSHOT={snapshot}
    install() {{ mkdir -p "${{@: -1}}"; }}
    chown() {{ :; }}
    chmod() {{ return 42; }}
    if migration_commit; then exit 99; fi
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr
    assert snapshot.exists()


def test_migration_commit_uses_fsynced_atomic_marker_replacement():
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("migration_commit() {") : text.index("ensure_split_storage_root() {")]

    assert "mktemp" in body
    assert "os.fsync" in body
    assert "os.replace" in body
    assert "os.open" in body
    assert 'cat > "$RUNTIME_IDENTITIES_FILE"' not in body


def test_split_migration_removes_only_legacy_user_acls_from_evidence(tmp_path):
    principal = os.environ.get("USER", "operator")
    storage = tmp_path / "storage"
    evidence_dir = storage / "dumps/job-1"
    evidence_dir.mkdir(parents=True)
    playground = storage / "playground"
    playground.mkdir()
    evidence = evidence_dir / "memory.dump"
    evidence.write_bytes(b"evidence")
    subprocess.run(["setfacl", "-m", f"u:{principal}:r--", evidence], check=True)
    subprocess.run(["setfacl", "-m", f"u:{principal}:--x", storage], check=True)
    subprocess.run(["setfacl", "-m", f"u:{principal}:--x", playground], check=True)
    outside = storage / "ssh_keys"
    outside.mkdir()
    outside_file = outside / "key"
    outside_file.write_text("key\n", encoding="utf-8")
    subprocess.run(
        ["setfacl", "-m", f"u:{principal}:r--", outside_file],
        check=True,
    )
    harness = f"""
    source {INSTALL}
    STORAGE_ROOT={storage}
    SERVICE_USER=$(id -un)
    remove_legacy_evidence_acls
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr
    evidence_acl = subprocess.run(["getfacl", "-cp", evidence], check=True, capture_output=True, text=True).stdout
    root_acl = subprocess.run(["getfacl", "-cp", storage], check=True, capture_output=True, text=True).stdout
    playground_acl = subprocess.run(["getfacl", "-cp", playground], check=True, capture_output=True, text=True).stdout
    evidence_dir_acl = subprocess.run(
        ["getfacl", "-cp", evidence_dir], check=True, capture_output=True, text=True
    ).stdout
    outside_acl = subprocess.run(["getfacl", "-cp", outside_file], check=True, capture_output=True, text=True).stdout
    assert f"user:{principal}:" not in evidence_acl
    assert f"user:{principal}:" not in root_acl
    assert f"user:{principal}:" not in playground_acl
    assert "group:astinuslab:r--" in evidence_acl
    assert "group:astinuslab:r-x" in evidence_dir_acl
    assert "group:astinuslab:--x" in root_acl
    assert "group:astinuslab:--x" in playground_acl
    assert f"user:{principal}:" in outside_acl
    assert "group:astinuslab:" not in outside_acl


def test_legacy_acl_cleanup_is_bounded_on_unsupported_fifo(tmp_path):
    storage = tmp_path / "storage"
    evidence_dir = storage / "dumps/job-1"
    evidence_dir.mkdir(parents=True)
    os.mkfifo(evidence_dir / "service-planted-fifo")
    harness = f"""
    source {INSTALL}
    STORAGE_ROOT={storage}
    SERVICE_USER=$(id -un)
    RUNNER_USER=astinuslab-runner
    remove_legacy_evidence_acls
    """

    result = subprocess.run(
        ["timeout", "2", "bash", "-c", harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_legacy_acl_cleanup_rejects_hardlinked_evidence(tmp_path):
    storage = tmp_path / "storage"
    evidence_dir = storage / "dumps/job-1"
    secret_dir = storage / "ssh_keys"
    evidence_dir.mkdir(parents=True)
    secret_dir.mkdir()
    secret = secret_dir / "private-key"
    secret.write_text("secret\n", encoding="utf-8")
    os.link(secret, evidence_dir / "memory.dump")
    harness = f"""
    source {INSTALL}
    STORAGE_ROOT={storage}
    SERVICE_USER=$(id -un)
    remove_legacy_evidence_acls
    """

    result = run_bash(harness)

    assert result.returncode != 0
    assert "multiply-linked evidence" in result.stderr
    secret_acl = subprocess.run(["getfacl", "-cp", secret], check=True, capture_output=True, text=True).stdout
    assert "group:astinuslab:" not in secret_acl


def test_migration_snapshot_copies_template_files_not_instance_names(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    for unit in ("astinuslab-api@.service", "astinuslab-runner@.service", "astinuslab-analysis@.service"):
        (unit_dir / unit).write_text(f"template {unit}\n", encoding="utf-8")
    empty_root = tmp_path / "empty-root"
    empty_root.mkdir()
    harness = f"""
    source {INSTALL}
    INSTALL_DIR={install}
    SERVICE_USER=operator
    SYSTEMD_UNIT_DIR={unit_dir}
    MIGRATION_SNAPSHOT_PARENT={tmp_path}
    SUDOERS_FILE={empty_root / "sudoers"}
    HELPER_INSTALL_DIR={empty_root / "libexec"}
    POLICY_FILE={empty_root / "policy"}
    RUNTIME_IDENTITIES_FILE={empty_root / "identities"}
    getfacl() {{ echo acl; }}
    systemctl() {{ return 1; }}
    id() {{ return 1; }}
    migration_snapshot
    for unit in astinuslab-api@.service astinuslab-runner@.service astinuslab-analysis@.service; do
      test -f "$MIGRATION_SNAPSHOT/units/$unit"
    done
    """
    result = run_bash(harness)
    assert result.returncode == 0, result.stderr


def test_service_control_always_restarts_and_never_starts():
    """`--restart` used to select between `start` and `restart`. Starting an
    already-active unit is a no-op, which is how an upgrade left old processes
    attached to a superseded release, so `restart` is now the only verb and the
    flag no longer changes what happens."""
    harness = f"""
    source {INSTALL}
    systemctl() {{ echo systemctl "$@"; }}
    SERVICE_USER=alice
    ENABLE_SERVICES=true
    START_SERVICES=true
    RESTART_SERVICES=false
    control_services
    RESTART_SERVICES=true
    control_services
    ENABLE_SERVICES=false
    START_SERVICES=false
    control_services
    """
    result = run_bash(harness)
    assert result.returncode == 0, result.stderr
    assert "systemctl enable astinuslab-api@alice.service" in result.stdout
    assert result.stdout.count("systemctl restart astinuslab-api@alice.service") == 2
    assert "systemctl start astinuslab-api@alice.service" not in result.stdout
    assert "enable --now" not in result.stdout


def test_ssh_key_flags_do_not_prompt_when_no_active_key(tmp_path):
    for args in ["--yes", "--generate-ssh-key", "--no-ssh-key", "--yes --no-ssh-key"]:
        install = tmp_path / args.replace(" ", "_").replace("-", "")
        script = f"""
        source {INSTALL}
        parse_args {args}
        INSTALL_DIR={install}
        SERVICE_USER=$(id -un)
        mkdir -p "$INSTALL_DIR/storage/ssh_keys"
        handle_ssh_key < /dev/null
        """
        result = run_bash(script)
        assert result.returncode == 0, result.stderr


def test_single_storage_reconciliation_removes_group_write_from_ssh_tree(tmp_path):
    install = tmp_path / "install"
    keys = install / "storage/ssh_keys"
    keys.mkdir(parents=True)
    private = keys / "id_ed25519"
    private.write_bytes(b"private\n")
    keys.chmod(0o2770)
    private.chmod(0o660)

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        SERVICE_USER=$(id -un)
        API_USER=$SERVICE_USER
        RUNNER_USER=$SERVICE_USER
        ANALYSIS_USER=$SERVICE_USER
        SPLIT_USERS=false
        MIGRATION_CREATED_DIRS={shlex.quote(str(tmp_path / "created"))}
        prepare_managed_directory() {{ :; }}
        secure_libvirt_image_access() {{ :; }}
        prepare_shared_databases() {{ :; }}
        prepare_storage
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert keys.stat().st_mode & 0o7777 == 0o700
    assert private.stat().st_mode & 0o777 == 0o600
    assert keys.stat().st_uid == os.getuid()
    assert private.stat().st_uid == os.getuid()


def test_active_ssh_key_requires_exact_runtime_custody_before_acceptance():
    """An active key is accepted only at the exact owner and group installed.

    The body moved to `ops/deploy/ssh_key.py`; the property is asserted where
    the code now lives. Root is deliberately not in the accepted set: a
    root-owned key in a service-reachable directory is a key the service cannot
    read, not a key the installer should adopt.
    """
    helper = SSH_KEY.read_text(encoding="utf-8")

    assert "allowed_owners = {0, owner}" not in helper
    assert "(private_info.st_uid, private_info.st_gid) != (owner, group)" in helper
    assert "(info.st_uid, info.st_gid) != (owner, group)" in helper


def test_the_installer_calls_the_extracted_ssh_key_module():
    text = INSTALL.read_text(encoding="utf-8")
    helper = text[text.index("inspect_or_generate_ssh_key()") : text.index("handle_ssh_key()")]

    assert 'python3 "$SCRIPT_DIR/ssh_key.py"' in helper
    assert '"$INSTALL_DIR" "$key_owner" astinuslab "$generate" "$key_directory_mode"' in helper
    assert "<<'PY'" not in helper


def test_ssh_key_setup_refuses_a_symlinked_active_marker_without_touching_target(tmp_path):
    keys = tmp_path / "install/storage/ssh_keys"
    keys.mkdir(parents=True)
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"operator-owned\n")
    (keys / ".active").symlink_to(sentinel)

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        SERVICE_USER=$(id -un)
        NO_SSH_KEY=true
        handle_ssh_key
    """)

    assert result.returncode != 0
    assert sentinel.read_bytes() == b"operator-owned\n"
    assert (keys / ".active").is_symlink()


def test_ssh_key_setup_refuses_a_symlinked_key_directory_without_touching_target(tmp_path):
    storage = tmp_path / "install/storage"
    outside = tmp_path / "outside"
    storage.mkdir(parents=True)
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside\n")
    (storage / "ssh_keys").symlink_to(outside, target_is_directory=True)

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        SERVICE_USER=$(id -un)
        NO_SSH_KEY=true
        handle_ssh_key
    """)

    assert result.returncode != 0
    assert sentinel.read_bytes() == b"outside\n"
    assert (storage / "ssh_keys").is_symlink()


def test_ssh_key_generation_never_overwrites_an_inactive_key(tmp_path):
    keys = tmp_path / "install/storage/ssh_keys"
    keys.mkdir(parents=True)
    private = keys / "astinuslab-default"
    public = keys / "astinuslab-default.pub"
    private.write_bytes(b"existing-private\n")
    public.write_bytes(b"existing-public\n")
    private.chmod(0o600)

    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(tmp_path / "install"))}
        SERVICE_USER=$(id -un)
        YES=true
        handle_ssh_key
    """)

    assert result.returncode != 0
    assert private.read_bytes() == b"existing-private\n"
    assert public.read_bytes() == b"existing-public\n"
    assert not (keys / ".active").exists()


def test_render_template_preserves_sed_sensitive_install_path(tmp_path):
    src = tmp_path / "unit.service"
    dst = tmp_path / "rendered.service"
    path = "/tmp/astinuslab-test-a&b|c\\d"
    src.write_text("ExecStart=/opt/astinuslab/.venv/bin/python @@INSTALL_DIR@@\n", encoding="utf-8")
    result = run_bash(f"source {INSTALL}; render_template {src} {dst} '{path}'")
    assert result.returncode == 0, result.stderr
    rendered = dst.read_text(encoding="utf-8")
    assert f"ExecStart={path}/.venv/bin/python {path}" in rendered


def test_check_mode_is_read_only_preflight(tmp_path):
    install = Path("/opt/astinuslab-preflight-read-only-test")
    result = subprocess.run(
        [
            str(INSTALL),
            "--check",
            "--install-dir",
            str(install),
            "--service-user",
            pwd.getpwuid(os.getuid()).pw_name,
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "ASTINUSLAB_POLICY_FILE": str(tmp_path / "missing-policy.json"),
            "ASTINUSLAB_SYSTEMD_UNIT_DIR": str(tmp_path / "units"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Preflight result: READY" in result.stdout
    assert not install.exists()


@pytest.mark.parametrize(
    "missing",
    ["cc", "virsh", "virt-install", "qemu-img", "cloud-localds", "pkg-config", "rsync", "ssh-keygen", "visudo"],
)
def test_preflight_fails_when_required_command_is_missing(tmp_path, missing):
    result = run_bash(
        f"""
        source {INSTALL}
        command() {{
          if [[ "$1" == -v && "$2" == {shlex.quote(missing)} ]]; then return 1; fi
          if [[ "$1" == -v ]]; then return 0; fi
          builtin command "$@"
        }}
        python3() {{ command python3 "$@"; }}
        pkg-config() {{ return 0; }}
        virsh() {{ return 0; }}
        validate_install_dir() {{ :; }}
        validate_existing_install_dir_state() {{ :; }}
        validate_existing_install_dir_authority() {{ :; }}
        resolve_storage_root() {{ STORAGE_ROOT="$INSTALL_DIR/storage"; }}
        validate_install_storage_root() {{ :; }}
        verify_storage_root_matches_config() {{ :; }}
        INSTALL_DIR=/opt/astinuslab-preflight-missing-test
        SERVICE_USER={shlex.quote(pwd.getpwuid(os.getuid()).pw_name)}
        POLICY_FILE={shlex.quote(str(tmp_path / "missing-policy.json"))}
        SYSTEMD_UNIT_DIR={shlex.quote(str(tmp_path / "units"))}
        run_preflight
        """
    )

    assert result.returncode != 0
    assert f"Missing command: {missing}" in result.stdout
    assert "Preflight result: NOT READY" in result.stdout


def _run_preflight_with_stubs(tmp_path, *, env=None):
    return run_bash(
        f"""
        source {INSTALL}
        command() {{
          if [[ "$1" == -v ]]; then return 0; fi
          builtin command "$@"
        }}
        python3() {{ command python3 "$@"; }}
        pkg-config() {{ return 0; }}
        virsh() {{ return 0; }}
        validate_install_dir() {{ :; }}
        validate_existing_install_dir_state() {{ :; }}
        validate_existing_install_dir_authority() {{ :; }}
        resolve_storage_root() {{ STORAGE_ROOT="$INSTALL_DIR/storage"; }}
        validate_install_storage_root() {{ :; }}
        verify_storage_root_matches_config() {{ :; }}
        INSTALL_DIR=/opt/astinuslab-preflight-virtiofsd-test
        SERVICE_USER={shlex.quote(pwd.getpwuid(os.getuid()).pw_name)}
        POLICY_FILE={shlex.quote(str(tmp_path / "missing-policy.json"))}
        SYSTEMD_UNIT_DIR={shlex.quote(str(tmp_path / "units"))}
        run_preflight
        """,
        env=env,
    )


def test_preflight_fails_when_virtiofsd_binary_is_missing(tmp_path):
    """libvirt starts virtiofsd itself for the job's managed virtiofs device, so a
    host without the daemon runs no jobs at all. The installer has to say that
    before it reports READY, instead of leaving it to the first job's traceback.
    """
    result = _run_preflight_with_stubs(
        tmp_path,
        env={"ASTINUSLAB_VIRTIOFSD_PATHS": f"{tmp_path}/absent-libexec:{tmp_path}/absent-qemu"},
    )

    assert result.returncode != 0
    assert "virtiofsd not found" in result.stdout
    assert "apt install virtiofsd" in result.stdout
    assert "Preflight result: NOT READY" in result.stdout


def test_preflight_accepts_virtiofsd_at_the_legacy_qemu_path(tmp_path):
    legacy = tmp_path / "qemu-virtiofsd"
    legacy.write_text("#!/bin/sh\n")
    legacy.chmod(0o755)

    result = _run_preflight_with_stubs(
        tmp_path,
        env={"ASTINUSLAB_VIRTIOFSD_PATHS": f"{tmp_path}/absent-libexec:{legacy}"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"virtiofsd present: {legacy}" in result.stdout


def test_check_prerequisites_fails_closed_when_virtiofsd_cannot_be_installed(tmp_path):
    """Only the Ubuntu package name is evidence-backed, so an unrecognised OS gets
    an actionable failure rather than a guessed package."""
    result = run_bash(
        f"""
        source {INSTALL}
        detect_os_family() {{ echo "unknown"; }}
        command() {{
          if [[ "$1" == -v ]]; then return 0; fi
          builtin command "$@"
        }}
        pkg-config() {{ return 0; }}
        virsh() {{ return 0; }}
        check_prerequisites
        """,
        env={"ASTINUSLAB_VIRTIOFSD_PATHS": f"{tmp_path}/absent-libexec:{tmp_path}/absent-qemu"},
    )

    assert result.returncode != 0
    assert "virtiofsd not found" in result.stderr
    assert "apt install virtiofsd" in result.stderr


@pytest.mark.parametrize("mode", ["missing", "present", "install-fails", "still-missing", "unknown-distro"])
def test_installer_requires_lightweight_attacker_boot_tool(tmp_path, mode):
    """Run real prerequisite control flow with inert package/host commands."""
    apt_log = tmp_path / "apt.log"
    result = run_bash(
        f"""
        source {INSTALL}
        virt_install_ready={"true" if mode == "present" else "false"}
        detect_os_family() {{ echo {"unknown" if mode == "unknown-distro" else "debian"}; }}
        command() {{
          if [[ "$1" == -v && "$2" == virt-install ]]; then $virt_install_ready; return; fi
          if [[ "$1" == -v ]]; then return 0; fi
          builtin command "$@"
        }}
        python3() {{ return 0; }}
        pkg-config() {{ return 0; }}
        virsh() {{ return 0; }}
        find_virtiofsd_binary() {{ echo /fixture/virtiofsd; }}
        apt-get() {{
          echo "$*" >> {shlex.quote(str(apt_log))}
          if [[ "$1" == install && " $* " == *" virtinst "* ]]; then
            {"return 1" if mode == "install-fails" else "virt_install_ready=true" if mode == "missing" else ":"}
          fi
          return 0
        }}
        check_prerequisites
        """
    )
    if mode in {"missing", "present"}:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "virt-install" in result.stderr
    if mode in {"present", "unknown-distro"}:
        assert not apt_log.exists(), "No package mutation is needed or supported in this case"
    else:
        operations = apt_log.read_text().splitlines()
        assert len(operations) == 2
        assert operations[0] == "update -qq"
        assert operations[1].split() == ["install", "-y", "-qq", "--no-install-recommends", "virtinst"]


def test_check_fails_for_untrusted_existing_install_root(tmp_path):
    install = tmp_path / "install"
    install.mkdir()

    result = subprocess.run(
        [
            str(INSTALL),
            "--check",
            "--install-dir",
            str(install),
            "--service-user",
            pwd.getpwuid(os.getuid()).pw_name,
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "ASTINUSLAB_POLICY_FILE": str(tmp_path / "missing-policy.json"),
            "ASTINUSLAB_SYSTEMD_UNIT_DIR": str(tmp_path / "units"),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "install-root authority" in (result.stdout + result.stderr)
    assert "Preflight result: NOT READY" in result.stdout


def test_check_prints_one_ready_verdict_for_supported_host(tmp_path):
    result = subprocess.run(
        [
            str(INSTALL),
            "--check",
            "--install-dir",
            "/opt/astinuslab-preflight-ready-test",
            "--service-user",
            pwd.getpwuid(os.getuid()).pw_name,
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "ASTINUSLAB_POLICY_FILE": str(tmp_path / "missing-policy.json"),
            "ASTINUSLAB_SYSTEMD_UNIT_DIR": str(tmp_path / "units"),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    verdicts = [line for line in result.stdout.splitlines() if line.startswith("Preflight result:")]
    assert verdicts == ["Preflight result: READY"]


def test_preflight_detects_privileged_policy_drift(tmp_path):
    policy = tmp_path / "acl-policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": "astinuslab-acl-policy/v1",
                "allowed_callers": ["operator"],
                "allowed_roots": [str(tmp_path / "other-storage")],
            }
        ),
        encoding="utf-8",
    )

    result = run_bash(
        f"""
        source {INSTALL}
        POLICY_FILE={shlex.quote(str(policy))}
        STORAGE_ROOT={shlex.quote(str(tmp_path / "storage"))}
        RUNNER_USER=operator
        preflight_policy_matches_storage
        """
    )

    assert result.returncode != 0
    assert "does not allow the resolved storage root" in result.stderr


def test_preflight_detects_installed_unit_drift_without_rewriting_it(tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    api_unit = unit_dir / "astinuslab-api@.service"
    original = b"[Unit]\nDescription=stale unit\n"
    api_unit.write_bytes(original)

    result = run_bash(
        f"""
        source {INSTALL}
        INSTALL_DIR=/opt/astinuslab-preflight-unit-drift-test
        STORAGE_ROOT="$INSTALL_DIR/storage"
        SERVICE_USER=operator
        SYSTEMD_UNIT_DIR={shlex.quote(str(unit_dir))}
        preflight_units_match
        """
    )

    assert result.returncode != 0
    assert api_unit.read_bytes() == original


def _write_forbidden_tool(path: Path, marker: Path) -> None:
    path.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' invoked >> '{marker}'\nexit 99\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _path_without_external_volatility_tools(tmp_path: Path) -> Path:
    tool_free_bin = tmp_path / "tool-free-bin"
    tool_free_bin.mkdir()
    for command in (
        "awk",
        "bash",
        "cat",
        "cc",
        "cloud-localds",
        "dirname",
        "id",
        "pkg-config",
        "python3",
        "qemu-img",
        "realpath",
        "rsync",
        "ssh-keygen",
        "virsh",
        "virt-install",
        "visudo",
    ):
        resolved = shutil.which(command)
        assert resolved is not None, f"test host is missing required command: {command}"
        (tool_free_bin / command).symlink_to(resolved)
    assert shutil.which("vol", path=str(tool_free_bin)) is None
    assert shutil.which("dwarf2json", path=str(tool_free_bin)) is None
    return tool_free_bin


def test_check_mode_succeeds_without_external_volatility_tools(tmp_path):
    tool_free_bin = _path_without_external_volatility_tools(tmp_path)

    result = subprocess.run(
        [
            str(INSTALL),
            "--check",
            "--install-dir",
            "/opt/astinuslab-preflight-no-vol-test",
            "--service-user",
            pwd.getpwuid(os.getuid()).pw_name,
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": str(tool_free_bin),
            "ASTINUSLAB_POLICY_FILE": str(tmp_path / "missing-policy.json"),
            "ASTINUSLAB_SYSTEMD_UNIT_DIR": str(tmp_path / "units"),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_check_mode_does_not_invoke_external_volatility_tools(tmp_path):
    fake_bin = tmp_path / "sentinel-bin"
    fake_bin.mkdir()
    marker = tmp_path / "external-tool-invoked"
    _write_forbidden_tool(fake_bin / "vol", marker)
    _write_forbidden_tool(fake_bin / "dwarf2json", marker)

    result = subprocess.run(
        [
            str(INSTALL),
            "--check",
            "--install-dir",
            "/opt/astinuslab-preflight-forbidden-vol-test",
            "--service-user",
            pwd.getpwuid(os.getuid()).pw_name,
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "ASTINUSLAB_POLICY_FILE": str(tmp_path / "missing-policy.json"),
            "ASTINUSLAB_SYSTEMD_UNIT_DIR": str(tmp_path / "units"),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "installer invoked an optional external Volatility tool"


def test_mocked_full_install_needs_no_external_volatility_tools(tmp_path):
    tool_free_bin = _path_without_external_volatility_tools(tmp_path)
    allowed_base = ROOT / ".pytest-mocked-install" / tmp_path.name
    shutil.rmtree(allowed_base, ignore_errors=True)
    install = allowed_base / "install"
    (install / "config").mkdir(parents=True)
    (install / "config/astinuslab.env").write_text("ASTINUSLAB_API_PORT=8000\n", encoding="utf-8")
    harness = f"""
    source {INSTALL}
    require_root() {{ :; }}
    validate_existing_install_dir_state() {{ :; }}
    validate_install_storage_root() {{ :; }}
    validate_existing_install_dir_authority() {{ :; }}
    refuse_foreign_template_instances() {{ :; }}
    check_prerequisites() {{ :; }}
    ensure_service_user() {{ :; }}
    install_source() {{ :; }}
    prepare_storage() {{ :; }}
    bind_storage_root_identity() {{ :; }}
    assert_storage_root_identity() {{ :; }}
    quiesce_services() {{ :; }}
    backup_database_before_migration() {{ :; }}
    apply_db_migrations() {{ :; }}
    verify_release_readiness() {{ :; }}
    handle_ssh_key() {{ :; }}
    install_sudoers_rule() {{ :; }}
    configure_mac() {{ :; }}
    install_systemd_units() {{ :; }}
    switch_release_and_restart() {{ :; }}
    migration_snapshot() {{ :; }}
    migration_snapshot_mutable_state() {{ :; }}
    prepare_single_upgrade_mutable_state() {{ :; }}
    migration_discard_snapshot() {{ :; }}
    cleanup_superseded_legacy_install() {{ :; }}
    main --install-dir {install} --service-user operator --yes
    """

    result = run_bash(
        harness,
        env={
            "PATH": str(tool_free_bin),
            "ASTINUSLAB_POLICY_FILE": str(tmp_path / "missing-policy.json"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "operator-supplied tools" in result.stdout
    assert "did not download or install them" in result.stdout
    assert "upstream licenses" in result.stdout
    assert "unavailable" in result.stdout
    positions = [result.stdout.index(phase) for phase in EXPECTED_INSTALL_PHASES]
    assert positions == sorted(positions)
    shutil.rmtree(allowed_base)


def test_phase_reporting_names_failure_before_rollback(tmp_path):
    result = run_bash(
        f"""
        source {INSTALL}
        migration_snapshot() {{ :; }}
        install_source() {{ :; }}
        quiesce_services() {{ :; }}
        migration_snapshot_mutable_state() {{ :; }}
        prepare_single_upgrade_mutable_state() {{ :; }}
        prepare_storage() {{ echo ORIGINAL-OPERATION-ERROR; return 1; }}
        release_rollback() {{ echo ROLLBACK-STARTED; }}
        migration_rollback() {{ :; }}
        upgrade_single_identity_install
        """
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "ORIGINAL-OPERATION-ERROR" in output
    assert "Failed during phase 5/9" in output
    assert output.index("Failed during phase 5/9") < output.index("ROLLBACK-STARTED")


def test_installer_has_no_external_volatility_download_or_install_path():
    source = INSTALL.read_text(encoding="utf-8")
    forbidden = {
        "pip install volatility3": r"pip[^\n]*install[^\n]*volatility3",
        "package-manager Volatility install": r"(?:apt(?:-get)?|dnf|yum|pacman|zypper)[^\n]*volatility",
        "curl downloader": r"(?:^|[;&|\s])curl\s",
        "wget downloader": r"(?:^|[;&|\s])wget\s",
        "git clone": r"git\s+clone",
        "dwarf2json build/download": r"(?:go\s+build|download)[^\n]*dwarf2json",
    }

    for label, pattern in forbidden.items():
        assert re.search(pattern, source, flags=re.IGNORECASE | re.MULTILINE) is None, (
            f"installer contains forbidden {label} behavior"
        )

    assert "--exclude='/vol3/bin/'" in source


def test_privileged_root_command_check_reports_policy_and_unit_mismatch_without_mutation(tmp_path):
    storage = ROOT / "storage"
    policy = tmp_path / "acl-policy.json"
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    result = run_bash(f"""
        source {PRIVILEGED_ROOTS}
        INSTALL_DIR={shlex.quote(str(ROOT))}
        SOURCE_DIR={shlex.quote(str(ROOT))}
        STORAGE_ROOT={shlex.quote(str(storage))}
        POLICY_FILE={shlex.quote(str(policy))}
        SYSTEMD_DIR={shlex.quote(str(unit_dir))}
        assert_install_source_authority() {{ :; }}
        check
    """)
    assert result.returncode != 0
    assert "mismatch" in result.stdout.lower()
    assert not policy.exists()
    assert not any(unit_dir.iterdir())


def test_privileged_root_command_renders_custom_storage_and_home_protection(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("mutation path is root-only; --check is covered without root")
    storage = ROOT / "storage"
    policy = tmp_path / "acl-policy.json"
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    result = subprocess.run(
        [
            str(PRIVILEGED_ROOTS),
            "--install-dir",
            str(ROOT),
            "--storage-root",
            str(storage),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "ASTINUSLAB_POLICY_FILE": str(policy),
            "ASTINUSLAB_SYSTEMD_DIR": str(unit_dir),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert str(storage) in policy.read_text(encoding="utf-8")
    for unit in ("astinuslab-api", "astinuslab-runner", "astinuslab-analysis"):
        rendered = (unit_dir / f"{unit}@.service").read_text(encoding="utf-8")
        # Bound, not listed read-write: see the render test below for why.
        assert f"BindPaths={storage}" in rendered
        assert f"ReadWritePaths={storage}" not in rendered
        assert "/opt/astinuslab/storage" not in rendered
    api = (unit_dir / "astinuslab-api@.service").read_text(encoding="utf-8")
    assert "ProtectHome=tmpfs" in api
    assert "custom storage root under /home" in api


def test_install_and_privileged_root_renderers_stay_in_sync(tmp_path):
    install_rendered = tmp_path / "install.service"
    roots_rendered = tmp_path / "roots.service"
    script = f"""
    source {INSTALL}
    render_template {ROOT / "ops/systemd/astinuslab-runner.service"} {install_rendered} {ROOT} /srv/astinuslab-storage
    source {PRIVILEGED_ROOTS}
    INSTALL_DIR={ROOT}
    STORAGE_ROOT=/srv/astinuslab-storage
    assert_install_source_authority() {{ :; }}
    render_unit astinuslab-runner {roots_rendered}
    cmp {install_rendered} {roots_rendered}
    """
    result = run_bash(script)
    assert result.returncode == 0, result.stderr
    rendered = install_rendered.read_text(encoding="utf-8")
    assert f"ReadOnlyPaths={ROOT}" in rendered
    assert "ReadWritePaths=/srv/astinuslab-storage" in rendered
    assert "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK" in rendered


def test_privileged_root_check_matches_explicit_split_units_and_runner_policy(tmp_path):
    storage = ROOT / "storage"
    storage_group = storage.stat().st_gid
    storage_groups = []
    if storage.stat().st_mode & 0o020:
        import grp

        storage_groups = [grp.getgrgid(storage_group).gr_name]
    policy = tmp_path / "acl-policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": "astinuslab-acl-policy/v1",
                "allowed_callers": ["astinuslab-runner"],
                "allowed_roots": [str(storage)],
                "storage_groups": storage_groups,
                "evidence_read_groups": ["astinuslab"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    marker = tmp_path / "runtime-identities"
    marker.write_text(
        "mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n",
        encoding="utf-8",
    )
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    render = f"""
    source {INSTALL}
    INSTALL_DIR={ROOT}
    RELEASE_DIR={ROOT}
    STORAGE_ROOT={storage}
    SPLIT_USERS=true
    API_USER=astinuslab-api
    RUNNER_USER=astinuslab-runner
    ANALYSIS_USER=astinuslab-analysis
    SYSTEMD_UNIT_DIR={unit_dir}
    systemctl() {{ :; }}
    install_systemd_units
    """
    rendered = run_bash(render)
    assert rendered.returncode == 0, rendered.stderr

    check_script = f"""
    source {PRIVILEGED_ROOTS}
    INSTALL_DIR={ROOT}
    STORAGE_ROOT={storage}
    POLICY_FILE={policy}
    SYSTEMD_DIR={unit_dir}
    RUNTIME_IDENTITIES_FILE={marker}
    split_identity_marker_is_valid() {{ return 0; }}
    assert_install_source_authority() {{ :; }}
    detect_runtime_identities
    json_matches() {{ return 0; }}
    check
    """
    result = run_bash(check_script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "policy and systemd units match" in result.stdout


def _json_matches_verdict(tmp_path: Path, *, storage_mode: int, storage_groups: list[str]) -> str:
    """Run json_matches against a real policy file and report its verdict.

    acl_policy refuses a policy that is not root-owned and refuses any root
    under /tmp, so the case runs in a user namespace with tmp_path bound over
    /srv - a registrable storage root. Inside that namespace the caller's own
    ids map to 0, so a group-writable root names group `root`.
    """
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    policy = tmp_path / "acl-policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": "astinuslab-acl-policy/v1",
                "allowed_callers": ["astinuslab-runner"],
                "allowed_roots": ["/srv/storage"],
                "storage_groups": storage_groups,
                "evidence_read_groups": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    policy.chmod(0o644)
    script = f"""
        set -euo pipefail
        mount --bind {shlex.quote(str(tmp_path))} /srv
        chmod {storage_mode:04o} /srv/storage
        source {PRIVILEGED_ROOTS}
        INSTALL_DIR={shlex.quote(str(ROOT))}
        SOURCE_DIR={shlex.quote(str(ROOT))}
        STORAGE_ROOT=/srv/storage
        POLICY_FILE=/srv/acl-policy.json
        SERVICE_USER=astinuslab-runner
        SPLIT_USERS=false
        assert_install_source_authority() {{ :; }}
        if json_matches; then echo matched; else echo mismatched; fi
    """
    result = subprocess.run(
        ["unshare", "--map-root-user", "--map-auto", "--mount", "bash", "-c", script],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "matched" in combined, combined
    return "mismatched" if "mismatched" in combined else "matched"


@pytest.mark.parametrize(
    "storage_mode, storage_groups, expected",
    [
        (0o2770, ["root"], "matched"),
        (0o2770, [], "mismatched"),
        (0o2770, ["astinuslab"], "mismatched"),
        (0o0750, ["root"], "mismatched"),
        (0o0750, [], "matched"),
    ],
)
def test_privileged_root_check_compares_storage_groups_against_the_live_root(
    tmp_path, storage_mode, storage_groups, expected
):
    """policy_payload derives storage_groups from the storage root's mode and
    gid, and the ACL helper refuses a group-writable path component that the
    policy does not name. json_matches skipped the field, so a root that gained
    group write (an acceptance rsync left it 2770 acceptance:astinuslab) after
    the policy was written kept passing --check with exit 0 and the reassuring
    'policy and systemd units match', while every job failed on that same
    component. --check must report the drift in both directions: a group the
    policy omits, and a group the live root does not justify."""
    assert _json_matches_verdict(tmp_path, storage_mode=storage_mode, storage_groups=storage_groups) == expected


def test_privileged_root_split_policy_keeps_evidence_group_separate_from_storage_group(tmp_path):
    output = tmp_path / "policy.json"
    storage = ROOT / "storage"
    harness = f"""
    source {PRIVILEGED_ROOTS}
    INSTALL_DIR={ROOT}
    STORAGE_ROOT={storage}
    SERVICE_USER=astinuslab-runner
    SPLIT_USERS=true
    assert_install_source_authority() {{ :; }}
    policy_payload > {output}
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["allowed_callers"] == ["astinuslab-runner"]
    assert payload["evidence_read_groups"] == ["astinuslab"]


def test_install_preserves_a_registered_custom_storage_root():
    """An operator who registers a custom storage root through
    ops/deploy/configure-privileged-roots.sh must keep it across the next upgrade.
    Re-rendering the policy and the systemd units from INSTALL_DIR/storage would
    silently revert it and leave every capture under the real root failing an
    ACL policy mismatch."""
    text = (ROOT / "ops" / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert "resolve_storage_root()" in text
    upgrade = text[text.index("upgrade_single_identity_install()") : text.index("upgrade_existing_split_install()")]
    assert upgrade.index("resolve_storage_root") < upgrade.index("install_sudoers_rule")
    assert '--storage-root "$STORAGE_ROOT"' in text
    assert 'render_template "$src" "$dst" "$INSTALL_DIR" "$STORAGE_ROOT"' in text
    # the default must remain a fallback, never the unconditional value
    assert '--storage-root "$INSTALL_DIR/storage"' not in text
    assert "/etc/astinuslab/acl-policy.json" in text


def test_uninstall_reports_user_deletion_only_after_success():
    text = UNINSTALL.read_text(encoding="utf-8")
    split_block = text[text.index("if $SPLIT_USERS; then", text.index("Optionally remove service user")) :]
    assert re.search(
        r'if userdel "\$split_user"; then\s+ok "User \$split_user deleted"\s+else\s+'
        r'warn "Could not delete split service user \$split_user"\s+fi',
        split_block,
    )


def test_admin_config_is_owned_by_the_service_user_after_the_atomic_rewrite():
    """configure_admin_auth replaces astinuslab.toml through os.replace, so the
    new inode carries the root-created temporary file's ownership and discards
    the chown applied in install_runtime_config_defaults. Every service reads
    that file at import (src/astinuslab/core/config.py), so leaving it root-owned 0600
    means nothing starts after a clean install."""
    text = (ROOT / "ops" / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert "secure_admin_config()" in text
    assert 'secure_managed_file astinuslab.toml "$config_owner" astinuslab "$config_mode"' in text
    assert 'config_owner="$API_USER"' in text
    assert "config_mode=0640" in text
    # both exits from configure_admin_auth must go through it
    assert text.count('secure_admin_config "$config_path"') >= 3
    # and never leave a bare chmod as the last word on the credential file
    assert 'PY\n  chmod 0600 "$config_path"' not in text


def _release_layout(tmp_path):
    """An installed root as Task 28 leaves it: the product source lives under
    releases/<id>/ and `current` points at the live one."""
    root = tmp_path / "opt"
    release = root / "releases" / "1.0.0-abcdef012345"
    (release / "ops" / "systemd").mkdir(parents=True)
    (release / "ops" / "host").mkdir(parents=True)
    (release / "ops" / "deploy").mkdir(parents=True)
    for unit in ("astinuslab-api", "astinuslab-runner", "astinuslab-analysis"):
        shutil.copy(ROOT / "ops/systemd" / f"{unit}.service", release / "ops/systemd" / f"{unit}.service")
    shutil.copy(ROOT / "ops/host/acl_policy.py", release / "ops/host" / "acl_policy.py")
    (root / "current").symlink_to(release)
    storage = root / "storage"
    storage.mkdir()
    return root, release, storage


def test_configure_privileged_roots_finds_the_source_inside_the_current_release(tmp_path):
    """Task 28 moved the product source into releases/<id>/, and this helper kept
    looking for ops/systemd directly under the install root. Registering a
    custom storage root on an installed host failed outright - the one path an
    operator has for moving evidence storage off the system disk."""
    root, _release, storage = _release_layout(tmp_path)

    result = run_bash(f"""
        set -euo pipefail
        source {PRIVILEGED_ROOTS}
        parse_args --install-dir {shlex.quote(str(root))} --storage-root {shlex.quote(str(storage))}
        echo "install=$INSTALL_DIR"
        echo "source=$SOURCE_DIR"
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"install={root}" in result.stdout, "the install root stays the data root"
    assert f"source={root}/current" in result.stdout, "the source is read through the live release"


@pytest.mark.parametrize("script", [INSTALL, PRIVILEGED_ROOTS])
def test_storage_root_identity_recheck_refuses_a_replaced_directory(tmp_path, script):
    storage = tmp_path / "storage"
    storage.mkdir()
    replacement = tmp_path / "replacement"
    result = run_bash(f"""
        source {script}
        STORAGE_ROOT={shlex.quote(str(storage))}
        validate_root_with_authority() {{ :; }}
        validate_install_storage_root() {{ :; }}
        bind_storage_root_identity
        mv -- {shlex.quote(str(storage))} {shlex.quote(str(replacement))}
        mkdir -- {shlex.quote(str(storage))}
        if assert_storage_root_identity; then echo accepted; else echo refused; fi
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "accepted" not in result.stdout
    assert "refused" in result.stdout


def test_storage_root_identity_is_rechecked_before_privileged_publication():
    install = INSTALL.read_text(encoding="utf-8")
    helper = PRIVILEGED_ROOTS.read_text(encoding="utf-8")

    assert install.count("assert_storage_root_identity") >= 4
    assert helper.index("assert_storage_root_identity") < helper.index('mv -f -- "$POLICY_TMP" "$POLICY_FILE"')
    publication = helper[helper.index('mv -f -- "$POLICY_TMP" "$POLICY_FILE"') :]
    assert publication.index("assert_storage_root_identity") < publication.index("configure_host_access")


@pytest.mark.parametrize("script", [INSTALL, PRIVILEGED_ROOTS])
@pytest.mark.parametrize("kind", ["regular", "masked"])
def test_privileged_unit_snapshot_round_trips_exact_type_and_mode(tmp_path, script, kind):
    unit = tmp_path / "astinuslab-api.service"
    backup = tmp_path / "backup"
    if kind == "regular":
        unit.write_text("[Unit]\nDescription=prior\n", encoding="utf-8")
        unit.chmod(0o640)
    else:
        unit.symlink_to("/dev/null")

    result = run_bash(f"""
        source {script}
        snapshot_managed_unit {shlex.quote(str(unit))} {shlex.quote(str(backup))}
        rm -f -- {shlex.quote(str(unit))}
        printf '[Unit]\nDescription=candidate\n' > {shlex.quote(str(unit))}
        restore_managed_unit {shlex.quote(str(backup))} {shlex.quote(str(unit))}
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    if kind == "regular":
        assert not unit.is_symlink()
        assert unit.read_text(encoding="utf-8") == "[Unit]\nDescription=prior\n"
        assert unit.stat().st_mode & 0o777 == 0o640
    else:
        assert unit.is_symlink()
        assert os.readlink(unit) == "/dev/null"


def test_installer_snapshot_covers_regular_masked_and_missing_units():
    text = INSTALL.read_text(encoding="utf-8")
    snapshot = text[
        text.index("local unit unit_file path") : text.index(
            "while IFS= read -r unit", text.index("local unit unit_file path")
        )
    ]

    assert "snapshot_managed_unit" in snapshot
    assert '[[ -e "$path" || -L "$path" ]]' in snapshot


def test_configure_privileged_roots_applies_reversible_host_access_from_the_live_release(tmp_path):
    """Registering a root after installation must do the same reversible
    AppArmor, SELinux, and qemu traversal work as the installer itself."""
    root, release, storage = _release_layout(tmp_path)
    log = tmp_path / "host-access.log"
    (release / "ops" / "deploy" / "install.sh").write_text(
        'configure_mac() { printf \'%s|%s\\n\' "$INSTALL_DIR" "$STORAGE_ROOT" > "$ASTINUSLAB_TEST_LOG"; }\n',
        encoding="utf-8",
    )

    result = run_bash(
        f"""
        set -euo pipefail
        source {PRIVILEGED_ROOTS}
        SOURCE_DIR={shlex.quote(str(release))}
        INSTALL_DIR={shlex.quote(str(root))}
        STORAGE_ROOT={shlex.quote(str(storage))}
        ASTINUSLAB_TEST_LOG={shlex.quote(str(log))}
        export ASTINUSLAB_TEST_LOG
        assert_install_source_authority() {{ :; }}
        configure_host_access
        """
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text(encoding="utf-8") == f"{root}|{storage}\n"


def test_installer_rejects_mixed_selinux_definitions_for_exact_expression(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    expression = subprocess.run(
        ["python3", "-c", "import re,sys; print(re.escape(sys.argv[1]) + r'(/.*)?')", str(storage)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    calls = tmp_path / "calls"
    result = run_bash(f"""
        source {INSTALL}
        STORAGE_ROOT={shlex.quote(str(storage))}
        ASTINUSLAB_APPARMOR_TEMPLATE={shlex.quote(str(tmp_path / "missing-apparmor"))}
        getenforce() {{ echo Enforcing; }}
        semanage() {{
          if [[ "$1 $2" == "fcontext -l" ]]; then
            printf '%s all files system_u:object_r:svirt_image_t:s0\n' {shlex.quote(expression)}
            printf '%s directory system_u:object_r:default_t:s0\n' {shlex.quote(expression)}
          else
            printf 'FORBIDDEN-semanage %s\n' "$*" >> {shlex.quote(str(calls))}
          fi
        }}
        restorecon() {{ echo FORBIDDEN-restorecon >> {shlex.quote(str(calls))}; }}
        grant_qemu_traversal() {{ echo FORBIDDEN-acl >> {shlex.quote(str(calls))}; }}
        configure_mac
    """)

    assert result.returncode != 0
    assert "does not use svirt_image_t" in result.stdout + result.stderr
    assert not calls.exists()


def test_configure_privileged_roots_still_resolves_a_flat_development_checkout(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()

    result = run_bash(f"""
        set -euo pipefail
        source {PRIVILEGED_ROOTS}
        parse_args --install-dir {shlex.quote(str(ROOT))} --storage-root {shlex.quote(str(storage))}
        echo "source=$SOURCE_DIR"
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"source={ROOT}\n" in result.stdout


def test_configure_privileged_roots_reads_its_python_helpers_from_the_release(tmp_path):
    """acl_policy.py ships inside the release too, so a PYTHONPATH pinned to the
    install root imports nothing and every policy call fails.

    The root here sits under /tmp, which the policy refuses on purpose, so what
    is asserted is which module answered - a refusal from the release means the
    import path is right, an ImportError means it is not."""
    root, _release, storage = _release_layout(tmp_path)

    result = run_bash(f"""
        set -euo pipefail
        source {PRIVILEGED_ROOTS}
        parse_args --install-dir {shlex.quote(str(root))} --storage-root {shlex.quote(str(storage))}
        assert_install_source_authority() {{ :; }}
        policy_payload
    """)

    assert "ModuleNotFoundError" not in result.stderr, "the policy module must resolve inside the release"
    assert f"{root}/current/ops/host/acl_policy.py" in result.stderr, "the live release answered"


def test_configure_privileged_roots_defaults_the_install_dir_above_the_release(tmp_path):
    """The documented invocation is the copy inside the release. Deriving the
    install root from the script's own directory would name `current` as the
    data root and render every unit against a path that moves each upgrade."""
    root, _release, _storage = _release_layout(tmp_path)
    shutil.copy(PRIVILEGED_ROOTS, root / "current" / "ops" / "deploy" / PRIVILEGED_ROOTS.name)

    result = run_bash(f"""
        set -euo pipefail
        source {shlex.quote(str(root))}/current/ops/deploy/configure-privileged-roots.sh
        echo "install=$INSTALL_DIR"
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"install={root}\n" in result.stdout


@pytest.mark.parametrize("marker_shape", ["symlink", "writable", "missing-group", "extra-line"])
def test_configure_privileged_roots_rejects_untrusted_or_inexact_runtime_marker(tmp_path, marker_shape):
    marker = tmp_path / "runtime-identities"
    exact = "mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n"
    if marker_shape == "symlink":
        target = tmp_path / "marker-target"
        target.write_text(exact, encoding="utf-8")
        marker.symlink_to(target)
    elif marker_shape == "missing-group":
        marker.write_text(exact.removesuffix("group=astinuslab\n"), encoding="utf-8")
    elif marker_shape == "extra-line":
        marker.write_text(exact + "mode=single\n", encoding="utf-8")
    else:
        marker.write_text(exact, encoding="utf-8")
        marker.chmod(0o666)
    systemd = tmp_path / "systemd"
    systemd.mkdir()

    result = run_bash(f"""
    source {PRIVILEGED_ROOTS}
    RUNTIME_IDENTITIES_FILE={shlex.quote(str(marker))}
    SYSTEMD_DIR={shlex.quote(str(systemd))}
    systemctl() {{ return 1; }}
    detect_runtime_identities
    echo FORBIDDEN-accepted
    """)

    assert result.returncode != 0
    assert "FORBIDDEN" not in result.stdout
    assert "malformed, or unprotected" in result.stderr


def test_configure_privileged_roots_rejects_markerless_explicit_split_units(tmp_path):
    marker = tmp_path / "missing-marker"
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    (systemd / "astinuslab-api.service").write_text("candidate\n", encoding="utf-8")

    result = run_bash(f"""
    source {PRIVILEGED_ROOTS}
    RUNTIME_IDENTITIES_FILE={shlex.quote(str(marker))}
    SYSTEMD_DIR={shlex.quote(str(systemd))}
    systemctl() {{ return 1; }}
    detect_runtime_identities
    echo FORBIDDEN-accepted
    """)

    assert result.returncode != 0
    assert "FORBIDDEN" not in result.stdout
    assert "explicit split units" in result.stderr


def test_configure_privileged_roots_cleanup_runs_after_main_returns(tmp_path):
    """The EXIT trap fires once main has returned, so cleanup state cannot be
    local to main: under `set -u` the trap aborts on the first out-of-scope name
    and deletes nothing, leaving temporary policy and unit backups behind in
    /etc and failing an otherwise successful run."""
    script = ROOT / "ops" / "deploy" / "configure-privileged-roots.sh"
    leftovers = [tmp_path / name for name in ("policy.tmp", "policy.backup", "unit.rendered", "unit.backup")]
    for path in leftovers:
        path.write_text("residue", encoding="utf-8")

    program = f"""
        set -euo pipefail
        source {script}
        POLICY_TMP={leftovers[0]}
        POLICY_BACKUP={leftovers[1]}
        RENDERED=({leftovers[2]})
        BACKUPS=({leftovers[3]})
        cleanup_artifacts
    """
    result = subprocess.run(["bash", "-c", program], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "unbound variable" not in result.stderr
    for path in leftovers:
        assert not path.exists(), f"{path.name} survived cleanup"


def test_configure_privileged_roots_cleanup_is_safe_with_nothing_to_remove():
    script = ROOT / "ops" / "deploy" / "configure-privileged-roots.sh"
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail; source {script}; cleanup_artifacts"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "unbound variable" not in result.stderr


def test_configure_privileged_roots_partial_unit_publish_restores_every_prior_state(tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    names = ["astinuslab-api@.service", "astinuslab-runner@.service", "astinuslab-analysis@.service"]
    api = unit_dir / names[0]
    runner = unit_dir / names[1]
    analysis = unit_dir / names[2]
    api.write_text("prior api bytes\n", encoding="utf-8")
    api.chmod(0o640)
    runner.symlink_to("/dev/null")
    rendered = []
    backups = []
    for index, name in enumerate(names):
        rendered_path = tmp_path / f"rendered-{index}"
        rendered_path.write_text(f"candidate {name}\n", encoding="utf-8")
        rendered.append(rendered_path)
        backups.append(tmp_path / f"backup-{index}")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_mv = fake_bin / "mv"
    fake_mv.write_text(
        '#!/usr/bin/env bash\nif [[ "${3:-}" == "$FAIL_PUBLISH_SOURCE" ]]; then exit 73; fi\nexec /usr/bin/mv "$@"\n',
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)

    result = run_bash(
        f"""
        source {PRIVILEGED_ROOTS}
        SYSTEMD_DIR={shlex.quote(str(unit_dir))}
        POLICY_FILE={shlex.quote(str(tmp_path / "policy"))}
        POLICY_BACKUP={shlex.quote(str(tmp_path / "policy-backup"))}
        POLICY_EXISTED=false
        SERVICE_USER=$(id -un)
        UNITS=(astinuslab-api astinuslab-runner astinuslab-analysis)
        RENDERED=({" ".join(shlex.quote(str(path)) for path in rendered)})
        BACKUPS=({" ".join(shlex.quote(str(path)) for path in backups)})
        BACKUP_EXISTED=(true true false)
        snapshot_managed_unit {shlex.quote(str(api))} {shlex.quote(str(backups[0]))}
        snapshot_managed_unit {shlex.quote(str(runner))} {shlex.quote(str(backups[1]))}
        : > {shlex.quote(str(backups[2]))}
        systemctl() {{ :; }}
        if publish_rendered_units; then exit 99; fi
        restore_previous_configuration
        """,
        env={
            "FAIL_PUBLISH_SOURCE": str(rendered[1]),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert api.read_text(encoding="utf-8") == "prior api bytes\n"
    assert api.stat().st_mode & 0o777 == 0o640
    assert runner.is_symlink() and os.readlink(runner) == "/dev/null"
    assert not analysis.exists() and not analysis.is_symlink()


def test_configure_privileged_roots_identity_race_restores_units_and_policy(tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    api = unit_dir / "astinuslab-api@.service"
    runner = unit_dir / "astinuslab-runner@.service"
    analysis = unit_dir / "astinuslab-analysis@.service"
    api.write_text("prior api bytes\n", encoding="utf-8")
    api.chmod(0o640)
    runner.symlink_to("/dev/null")
    policy = tmp_path / "policy"
    policy.write_text("prior policy bytes\n", encoding="utf-8")
    policy.chmod(0o600)
    rendered = []
    backups = []
    for index in range(3):
        rendered_path = tmp_path / f"rendered-{index}"
        rendered_path.write_text(f"candidate unit {index}\n", encoding="utf-8")
        rendered.append(rendered_path)
        backups.append(tmp_path / f"backup-{index}")
    policy_backup = tmp_path / "policy-backup"

    result = run_bash(f"""
        source {PRIVILEGED_ROOTS}
        SYSTEMD_DIR={shlex.quote(str(unit_dir))}
        POLICY_FILE={shlex.quote(str(policy))}
        POLICY_BACKUP={shlex.quote(str(policy_backup))}
        POLICY_EXISTED=true
        SERVICE_USER=$(id -un)
        UNITS=(astinuslab-api astinuslab-runner astinuslab-analysis)
        RENDERED=({" ".join(shlex.quote(str(path)) for path in rendered)})
        BACKUPS=({" ".join(shlex.quote(str(path)) for path in backups)})
        BACKUP_EXISTED=(true true false)
        snapshot_managed_unit {shlex.quote(str(api))} {shlex.quote(str(backups[0]))}
        snapshot_managed_unit {shlex.quote(str(runner))} {shlex.quote(str(backups[1]))}
        : > {shlex.quote(str(backups[2]))}
        cp -a -- {shlex.quote(str(policy))} {shlex.quote(str(policy_backup))}
        systemctl() {{ :; }}
        assert_storage_root_identity() {{ return 1; }}
        if publish_rendered_configuration; then exit 99; fi
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert api.read_text(encoding="utf-8") == "prior api bytes\n"
    assert api.stat().st_mode & 0o777 == 0o640
    assert runner.is_symlink() and os.readlink(runner) == "/dev/null"
    assert not analysis.exists() and not analysis.is_symlink()
    assert policy.read_text(encoding="utf-8") == "prior policy bytes\n"
    assert policy.stat().st_mode & 0o777 == 0o600


def test_configure_privileged_roots_rolls_back_only_new_mac_records(tmp_path):
    script = ROOT / "ops" / "deploy" / "configure-privileged-roots.sh"
    template = tmp_path / "TEMPLATE.qemu"
    old_rule = '  "/srv/operator/**" rwk,'
    new_rule = '  "/srv/astinuslab/**" rwk,'
    template.write_text(f"profile test {{\n{old_rule}\n{new_rule}\n}}\n", encoding="utf-8")
    template.chmod(0o644)
    mac_manifest = tmp_path / "mac-policy.manifest"
    old_record = json.dumps(
        {"kind": "apparmor", "target": str(template), "value": old_rule, "version": 1}, sort_keys=True
    )
    new_record = json.dumps(
        {"kind": "apparmor", "target": str(template), "value": new_rule, "version": 1}, sort_keys=True
    )
    mac_manifest.write_text(old_record + "\n", encoding="utf-8")
    mac_manifest.chmod(0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    systemctl.chmod(0o755)

    # The rollback now requires the template and its parent to be root-owned, the
    # same custody gate the forward path in install.sh applies, so this runs in a
    # namespace where the fixture files read as root's.
    result = run_root_mapped_bash(
        f"""
        source {script}
        STORAGE_ROOT=/srv/astinuslab
        snapshot_host_access_manifests
        printf '%s\n' {shlex.quote(new_record)} >> "$MAC_POLICY_MANIFEST"
        rollback_host_access_delta
        cleanup_artifacts
        """,
        env={
            "ASTINUSLAB_MAC_POLICY_MANIFEST": str(mac_manifest),
            "ASTINUSLAB_TRAVERSAL_ACL_MANIFEST": str(tmp_path / "acl.manifest"),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert mac_manifest.read_text(encoding="utf-8") == old_record + "\n"
    rendered = template.read_text(encoding="utf-8")
    assert old_rule in rendered
    assert new_rule not in rendered


def test_configure_rollback_removes_apparmor_rule_recreated_from_stale_record(tmp_path):
    script = ROOT / "ops" / "deploy" / "configure-privileged-roots.sh"
    template = tmp_path / "TEMPLATE.qemu"
    template.write_text("profile test {\n}\n", encoding="utf-8")
    template.chmod(0o644)
    storage = tmp_path / "storage"
    storage.mkdir()
    rule = f'  "{storage}/**" rwk,'
    record = json.dumps(
        {"kind": "apparmor", "target": str(template), "value": rule, "version": 2},
        sort_keys=True,
    )
    mac_manifest = tmp_path / "mac-policy.manifest"
    mac_manifest.write_text(record + "\n", encoding="utf-8")
    mac_manifest.chmod(0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    systemctl.chmod(0o755)

    result = run_root_mapped_bash(
        f"""
        source {script}
        STORAGE_ROOT={shlex.quote(str(storage))}
        snapshot_host_access_manifests
        grant_qemu_traversal() {{ :; }}
        configure_host_access() {{
            INSTALL_DIR="$INSTALL_DIR" STORAGE_ROOT="$STORAGE_ROOT" \
              ASTINUSLAB_TRAVERSAL_ACL_MANIFEST="$TRAVERSAL_ACL_MANIFEST" \
              ASTINUSLAB_MAC_POLICY_MANIFEST="$MAC_POLICY_MANIFEST" \
              ASTINUSLAB_HOST_ACCESS_TRANSACTION_LOG="$HOST_ACCESS_TRANSACTION_LOG" \
              bash -euo pipefail -c 'source "$1"; grant_qemu_traversal() {{ :; }}; configure_mac' \
                test {INSTALL}
        }}
        configure_host_access
        grep -Fqx -- {shlex.quote(rule)} {shlex.quote(str(template))}
        rollback_host_access_delta
        """,
        env={
            "ASTINUSLAB_APPARMOR_TEMPLATE": str(template),
            "ASTINUSLAB_MAC_POLICY_MANIFEST": str(mac_manifest),
            "ASTINUSLAB_TRAVERSAL_ACL_MANIFEST": str(tmp_path / "acl.manifest"),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert rule not in template.read_text(encoding="utf-8")
    assert mac_manifest.read_text(encoding="utf-8") == record + "\n"


def _mac_delta_rollback_harness(record: str, storage_root: str) -> str:
    """Append one MAC record as a delta and roll it back.

    `snapshot_host_access_manifests` fixes the baseline first, so the appended
    record is the only line the rollback is allowed to revoke.
    """
    return f"""
        source {PRIVILEGED_ROOTS}
        STORAGE_ROOT={shlex.quote(storage_root)}
        snapshot_host_access_manifests
        umask 077
        printf '%s\\n' {shlex.quote(record)} >> "$MAC_POLICY_MANIFEST"
        rollback_host_access_delta
    """


@pytest.mark.parametrize("unsafe_custody", ["writable", "unprivileged"])
def test_configure_rollback_refuses_an_unprotected_apparmor_template(tmp_path, unsafe_custody):
    """Rollback edits the same root-owned policy file uninstall.sh edits, and it
    reloads the kernel's MAC policy afterwards. It must apply the same custody
    gate: a template that is no longer root-owned, or is group/other-writable, is
    left alone rather than rewritten and handed to `systemctl restart`."""
    template = tmp_path / "TEMPLATE.qemu"
    rule = '  "/srv/astinuslab/**" rwk,'
    original = f"profile test {{\n{rule}\n}}\n"
    template.write_text(original, encoding="utf-8")
    template.chmod(0o644)
    mac_manifest = tmp_path / "mac-policy.manifest"
    mac_manifest.write_text("", encoding="utf-8")
    mac_manifest.chmod(0o600)
    record = json.dumps({"kind": "apparmor", "target": str(template), "value": rule, "version": 2}, sort_keys=True)
    reload_calls = tmp_path / "reload.calls"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(reload_calls))}\n", encoding="utf-8")
    systemctl.chmod(0o755)
    _require_fakeroot()
    unsafe_setup = _unsafe_custody_setup(template, unsafe_custody)

    result = run_fakeroot_bash(
        f"{unsafe_setup}\n{_mac_delta_rollback_harness(record, '/srv/astinuslab')}",
        env={
            "ASTINUSLAB_MAC_POLICY_MANIFEST": str(mac_manifest),
            "ASTINUSLAB_TRAVERSAL_ACL_MANIFEST": str(tmp_path / "acl.manifest"),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "AppArmor template is not a protected root-owned file" in result.stderr, result.stderr
    assert template.read_text(encoding="utf-8") == original
    assert not reload_calls.exists(), "an unverified template must never reach a policy reload"
    assert record in mac_manifest.read_text(encoding="utf-8"), "an unrevoked record stays pending"


def test_configure_rollback_refuses_an_unprotected_apparmor_parent(tmp_path):
    """A group/other-writable parent directory means the final path component can
    be swapped under the rollback, so the edit is refused before it starts."""
    policy_dir = tmp_path / "apparmor.d"
    policy_dir.mkdir()
    template = policy_dir / "TEMPLATE.qemu"
    rule = '  "/srv/astinuslab/**" rwk,'
    original = f"profile test {{\n{rule}\n}}\n"
    template.write_text(original, encoding="utf-8")
    template.chmod(0o644)
    policy_dir.chmod(0o777)
    mac_manifest = tmp_path / "mac-policy.manifest"
    mac_manifest.write_text("", encoding="utf-8")
    mac_manifest.chmod(0o600)
    record = json.dumps({"kind": "apparmor", "target": str(template), "value": rule, "version": 2}, sort_keys=True)
    reload_calls = tmp_path / "reload.calls"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(reload_calls))}\n", encoding="utf-8")
    systemctl.chmod(0o755)

    result = run_root_mapped_bash(
        _mac_delta_rollback_harness(record, "/srv/astinuslab"),
        env={
            "ASTINUSLAB_MAC_POLICY_MANIFEST": str(mac_manifest),
            "ASTINUSLAB_TRAVERSAL_ACL_MANIFEST": str(tmp_path / "acl.manifest"),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "AppArmor template parent is not a protected root-owned directory" in result.stderr, result.stderr
    assert template.read_text(encoding="utf-8") == original
    assert not reload_calls.exists(), "an unverified template must never reach a policy reload"


def test_configure_rollback_refuses_apparmor_template_replacement_before_rename(tmp_path):
    """The published edit has to land on the inode the rollback validated. A
    template swapped while the replacement is being written is not rewritten, and
    the operator's file is left holding the rule rather than the rollback's text."""
    policy_dir = tmp_path / "apparmor.d"
    policy_dir.mkdir()
    # pytest runs under umask 002, so an unadjusted mkdir is group-writable and
    # the custody guard would refuse the parent before reaching the race.
    policy_dir.chmod(0o755)
    template = policy_dir / "TEMPLATE.qemu"
    displaced = tmp_path / "original-template"
    rule = '  "/srv/astinuslab/**" rwk,'
    template.write_text(f"profile test {{\n{rule}\n}}\n", encoding="utf-8")
    template.chmod(0o644)
    mac_manifest = tmp_path / "mac-policy.manifest"
    mac_manifest.write_text("", encoding="utf-8")
    mac_manifest.chmod(0o600)
    record = json.dumps({"kind": "apparmor", "target": str(template), "value": rule, "version": 2}, sort_keys=True)
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
        _mac_delta_rollback_harness(record, "/srv/astinuslab"),
        env={
            "ASTINUSLAB_MAC_POLICY_MANIFEST": str(mac_manifest),
            "ASTINUSLAB_TRAVERSAL_ACL_MANIFEST": str(tmp_path / "acl.manifest"),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PYTHONPATH": str(fake_modules),
            "APPARMOR_RACE_TARGET": str(template),
            "APPARMOR_RACE_DISPLACED": str(displaced),
        },
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert template.read_text(encoding="utf-8") == "profile replacement {\n}\n"
    assert rule in displaced.read_text(encoding="utf-8")
    assert record in mac_manifest.read_text(encoding="utf-8"), "an unrevoked record stays pending"


def test_configure_rollback_relabels_the_root_the_selinux_expression_covers(tmp_path):
    """A version-1 record carries no `target`, so the relabel root comes from the
    expression being deleted -- as uninstall.sh derives it. A version-1 record
    cannot have been written by this run, which only writes version 2, so the
    run's own STORAGE_ROOT is not the root that expression covers."""
    recorded = tmp_path / "recorded-storage"
    recorded.mkdir()
    current = tmp_path / "current-storage"
    current.mkdir()
    mac_manifest = tmp_path / "mac-policy.manifest"
    expression = f"{recorded}(/.*)?"
    record = json.dumps({"kind": "selinux", "target": "", "value": expression, "version": 1}, sort_keys=True)
    call_log = tmp_path / "calls.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    semanage = fake_bin / "semanage"
    semanage.write_text(
        '#!/bin/sh\nprintf "semanage %s\\n" "$*" >> "$ROLLBACK_CALL_LOG"\n'
        'if [ "$1 $2" = "fcontext -l" ]; then printf "%s all files system_u:object_r:svirt_image_t:s0\\n" "$SELINUX_EXPRESSION"; fi\n',
        encoding="utf-8",
    )
    semanage.chmod(0o755)
    restorecon = fake_bin / "restorecon"
    restorecon.write_text('#!/bin/sh\nprintf "restorecon %s\\n" "$*" >> "$ROLLBACK_CALL_LOG"\n', encoding="utf-8")
    restorecon.chmod(0o755)

    result = run_bash(
        _mac_delta_rollback_harness(record, str(current)),
        env={
            "ASTINUSLAB_MAC_POLICY_MANIFEST": str(mac_manifest),
            "ASTINUSLAB_TRAVERSAL_ACL_MANIFEST": str(tmp_path / "acl.manifest"),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "ROLLBACK_CALL_LOG": str(call_log),
            "SELINUX_EXPRESSION": expression,
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert f"semanage fcontext -d {expression}" in calls
    assert f"restorecon -Rv {recorded}" in calls, "the deleted expression's own root is relabelled"
    assert f"restorecon -Rv {current}" not in calls, "the current run's storage root is not the recorded one"


def test_configure_privileged_roots_relabels_after_selinux_delta_rollback(tmp_path):
    script = ROOT / "ops" / "deploy" / "configure-privileged-roots.sh"
    storage = tmp_path / "storage"
    storage.mkdir()
    mac_manifest = tmp_path / "mac-policy.manifest"
    expression = f"{storage}(/.*)?"
    record = json.dumps({"kind": "selinux", "target": "", "value": expression, "version": 1}, sort_keys=True)
    call_log = tmp_path / "calls.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    semanage = fake_bin / "semanage"
    semanage.write_text(
        '#!/bin/sh\nprintf "semanage %s\\n" "$*" >> "$ROLLBACK_CALL_LOG"\n'
        'if [ "$1 $2" = "fcontext -l" ]; then printf "%s all files system_u:object_r:svirt_image_t:s0\\n" "$SELINUX_EXPRESSION"; fi\n',
        encoding="utf-8",
    )
    semanage.chmod(0o755)
    restorecon = fake_bin / "restorecon"
    restorecon.write_text('#!/bin/sh\nprintf "restorecon %s\\n" "$*" >> "$ROLLBACK_CALL_LOG"\n', encoding="utf-8")
    restorecon.chmod(0o755)

    result = run_bash(
        f"""
            source {script}
            STORAGE_ROOT={shlex.quote(str(storage))}
            snapshot_host_access_manifests
            umask 077
            printf '%s\n' {shlex.quote(record)} >> "$MAC_POLICY_MANIFEST"
        rollback_host_access_delta
        cleanup_artifacts
        """,
        env={
            "ASTINUSLAB_MAC_POLICY_MANIFEST": str(mac_manifest),
            "ASTINUSLAB_TRAVERSAL_ACL_MANIFEST": str(tmp_path / "acl.manifest"),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "ROLLBACK_CALL_LOG": str(call_log),
            "SELINUX_EXPRESSION": expression,
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert f"semanage fcontext -d {expression}" in calls
    assert f"restorecon -Rv {storage}" in calls
    assert not mac_manifest.exists()


def test_configure_privileged_roots_restores_the_previous_service_runtime_state(tmp_path):
    call_log = tmp_path / "systemctl.log"
    result = run_bash(f"""
        source {PRIVILEGED_ROOTS}
        SERVICE_USER=operator
        UNITS=(astinuslab-api astinuslab-runner)
        SERVICE_WAS_ACTIVE=(true false)
        systemctl() {{
          if [[ "$1" == is-active ]]; then [[ "$3" == astinuslab-runner@operator.service ]]; return; fi
          printf '%s\n' "$*" >> {shlex.quote(str(call_log))}
        }}
        restore_previous_runtime
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "restart astinuslab-api@operator.service",
        "stop astinuslab-runner@operator.service",
    ]


def test_configure_privileged_roots_failure_paths_restore_absence_and_host_access():
    text = PRIVILEGED_ROOTS.read_text(encoding="utf-8")

    assert "BACKUP_EXISTED" in text
    assert 'rm -f -- "$SYSTEMD_DIR/$(unit_filename "$unit")"' in text
    assert text.count("rollback_host_access_delta") >= 3, "definition plus configure/restart failure paths"


def test_configure_privileged_roots_restart_requires_stable_deep_readiness():
    text = PRIVILEGED_ROOTS.read_text(encoding="utf-8")
    restart = text[text.index("if $RESTART; then") : text.index('echo "configured privileged storage root')]

    assert "verify_restarted_runtime" in restart
    assert restart.index("verify_restarted_runtime") < restart.index("rollback_host_access_delta")
    assert restart.index("verify_restarted_runtime") < restart.index("restore_previous_configuration")


def test_configure_privileged_roots_propagates_deep_readiness_failure():
    result = run_bash(f"""
        source {PRIVILEGED_ROOTS}
        SERVICE_USER=operator
        UNITS=(astinuslab-api astinuslab-runner astinuslab-analysis)
        [[ "$(type -t verify_restarted_runtime)" == function ]] || exit 88
        systemctl() {{ [[ "$1" == is-active ]]; }}
        sleep() {{ :; }}
        run_configured_deep_readiness() {{ return 42; }}
        if verify_restarted_runtime; then exit 99; fi
    """)

    assert result.returncode == 0, result.stdout + result.stderr


def test_sync_source_tree_ships_every_application_python_module(tmp_path):
    """The installed tree must be able to import the app it installed.

    `--exclude='storage/*'` was unanchored, so rsync applied it at every depth and
    `src/astinuslab/storage/` arrived empty. `astinuslab.main` imports
    `astinuslab.storage.keystore`, so every installed release since the initial
    public commit could not start its API - and no fake-mode test could see it,
    because those run from the source checkout rather than the installed tree.
    """
    if not shutil.which("rsync"):
        pytest.skip("rsync is required to exercise the sync path")
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    install = tmp_path / "install"

    # An application package that happens to be named like a runtime directory.
    (repo / "src/astinuslab/storage").mkdir(parents=True)
    (repo / "src/astinuslab/storage/__init__.py").write_text("", encoding="utf-8")
    (repo / "src/astinuslab/storage/keystore.py").write_text("KEY = 1\n", encoding="utf-8")
    (repo / "src/astinuslab").joinpath("main.py").write_text("from .storage import keystore\n", encoding="utf-8")
    # The runtime storage directory at the repo root, which must NOT be copied.
    (repo / "storage/dumps").mkdir(parents=True)
    (repo / "storage/dumps/evidence.bin").write_text("operator evidence\n", encoding="utf-8")

    result = run_bash(f"source {INSTALL}; REPO_DIR={repo}; INSTALL_DIR={install}; sync_source_tree")

    assert result.returncode == 0, result.stderr
    assert (install / "src/astinuslab/storage/keystore.py").read_text(encoding="utf-8") == "KEY = 1\n"
    assert (install / "src/astinuslab/storage/__init__.py").exists()
    assert not (install / "storage/dumps/evidence.bin").exists(), "runtime storage must stay out of the install"


def _synced_paths(destination: Path) -> set[str]:
    """Every path `sync_source_tree` would copy out of *this* repository.

    A dry run against the real tree, not a synthetic one: an exclude pattern is
    only wrong in the presence of the files it meets, and the synthetic fixtures
    above cannot contain a file nobody thought to create.
    """
    listing = subprocess.run(
        ["rsync", "-a", "--dry-run", "--out-format=%n", *_sync_exclude_arguments(), "./", f"{destination}/"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return {line.strip().rstrip("/") for line in listing}


def _tracked(*paths: str) -> list[str]:
    return subprocess.run(
        ["git", "ls-files", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()


# An rsync pattern is anchored to the transfer root only by a leading '/'. A
# mid-pattern slash does not anchor it: `config/catalog/*.yaml` matches that
# suffix at every depth, the same as a bare name does. Unanchored is often the
# intent -- `__pycache__` should die wherever it grows -- but it is also how the
# release-blocking defect shipped: a bare `storage` emptied the application's own
# `storage` package as well as the operator's data directory.
#
# So every unanchored pattern is ratified here, with the reason it is safe to
# apply at every depth. This list records the intent;
# `test_installed_tree_would_contain_every_runtime_asset` is what proves each
# one still holds against the real tree.
UNANCHORED_BY_DESIGN: dict[str, str] = {
    # Build and tool residue. Legitimately nested, and never part of a release.
    ".venv*": "a virtualenv at any depth is dev residue; the glob also catches .venv313",
    "__pycache__": "bytecode caches nest wherever a package does",
    "*.pyc": "bytecode nests wherever a package does",
    ".pytest_cache": "test-run residue, written wherever pytest was invoked",
    ".mypy_cache": "type-check residue, written wherever mypy was invoked",
    ".ruff_cache": "lint residue, written wherever ruff was invoked",
    "node_modules": "npm trees nest, and none of the frontend build ships",
    ".git": "git metadata, including any nested repository or submodule",
    "astinuslab.egg-info": "editable-install metadata, regenerated by the install",
    # Agent and forge state. Names are reserved by their tools, so a nested one
    # is the same kind of residue as one at the root.
    ".claude": "agent state; the name is reserved and never a runtime directory",
    ".github": "forge configuration, tracked but never executed on a host",
    ".hermes": "agent state; untracked working-tree residue",
    ".superpowers": "agent state; untracked working-tree residue",
    # Internal working material. No shipped tree uses these names, which is what
    # makes matching at every depth safe rather than merely convenient.
    "plans": "internal planning material, kept outside the public repository",
    "docs": "documentation; the published subset is re-included by anchored /docs patterns above",
    "tests": "no test tree ships, at the root or nested",
    "testdata": "fixtures for tests, which do not ship",
    "to-do": "internal working notes",
    "troubleshooting": "internal working notes",
    # Path-suffix patterns. Unanchored, but the leading segment is specific
    # enough that no second directory in the tree can collide with it.
    "images/manifests": "provenance notes for images, not needed to run one",
    "images/definitions/*": "the operator's own image registrations, which the install must not overwrite",
    "astinuslab.toml": "the operator's live config; install_config_if_missing seeds it from the example",
    "config/astinuslab.env": "the operator's live launcher env; seeded the same way",
    "config/catalog/*.yaml": "live catalogs are operator state; the .example.yaml include above wins for seeds",
    "config/catalog/*.yml": "same, for the .yml spelling",
    "config/catalog/*.example.yaml": "re-includes the seeds ahead of the live-catalog exclude",
    "config/catalog/*.example.yml": "same, for the .yml spelling",
}


def test_every_unanchored_sync_pattern_is_ratified():
    """A new unanchored pattern must say why it is safe at every depth.

    Anchoring is one character, and the cost of getting it wrong is an installed
    tree that cannot boot. Adding `--exclude='cache'` to kill one build directory
    also deletes any `cache` package under `src/astinuslab`; this fails until the
    author either anchors it or writes down why every depth is intended.
    """
    patterns = [argument.split("=", 1)[1] for argument in _sync_exclude_arguments()]
    assert patterns, "no rsync patterns parsed from install.sh"

    unratified = [p for p in patterns if not p.startswith("/") and p not in UNANCHORED_BY_DESIGN]
    assert not unratified, (
        "unanchored rsync patterns in sync_source_tree match at every depth. Anchor each with a "
        "leading '/', or add it to UNANCHORED_BY_DESIGN with the reason every depth is intended:\n  "
        + "\n  ".join(unratified)
    )

    stale = [p for p in UNANCHORED_BY_DESIGN if p not in patterns]
    assert not stale, (
        "UNANCHORED_BY_DESIGN ratifies patterns install.sh no longer passes; drop them:\n  " + "\n  ".join(stale)
    )

    unexplained = [p for p, reason in UNANCHORED_BY_DESIGN.items() if not reason.strip()]
    assert not unexplained, "ratified without a reason:\n  " + "\n  ".join(unexplained)


def test_installed_tree_would_contain_every_tracked_application_module(tmp_path):
    """Guards the class, not just the one path that broke.

    Any future exclude that swallows application code fails here rather than on
    an operator's host at first start.
    """
    if not shutil.which("rsync"):
        pytest.skip("rsync is required to exercise the sync path")
    synced = _synced_paths(tmp_path / "probe")
    missing = [path for path in _tracked("src/astinuslab") if path.endswith(".py") and path not in synced]

    assert not missing, f"application modules the installer would not ship: {missing}"


# Trees the running installation reads out of `settings.base_dir`. Python is
# covered by the test above; these are everything else the product needs on
# disk, and none of it is a `.py` file, so none of it was covered before.
#
#   src/astinuslab  Jinja templates, static JS/CSS/fonts, Windows guest assets
#   ops/host        host helpers, named in virtualization/host_script_manifest.py
#   ops/deploy      the uninstaller and privileged-root helper, upgraded in place
#   ops/systemd     unit templates the installer renders
#   guest           cloud-init templates, guest scripts, workload installers
#   vol3/scripts    symbol build, invoked from forensics/vol3_support.py
#   config          the `.example` seeds install_config_if_missing copies
RUNTIME_TREES = (
    "src/astinuslab",
    "ops/host",
    "ops/deploy",
    "ops/systemd",
    "guest",
    "vol3/scripts",
    "config",
)


def test_installed_tree_would_contain_every_runtime_asset(tmp_path):
    """Templates, static assets and helper scripts, not just importable modules.

    Every exclude in `sync_source_tree` except nine is unanchored, so each one
    matches at *every* depth. `--exclude='docs'` would take
    `src/astinuslab/web/static/docs/` with it; `--exclude='tests'` would take a
    `tests` directory added anywhere under the package. Neither is hypothetical
    -- the unanchored `--exclude='storage'` that this pattern list now anchors
    emptied the application's own `storage` package and left the installed tree
    unable to boot.

    The `.py`-only check above would not have seen any of that, because none of
    the assets here is a `.py` file.
    """
    if not shutil.which("rsync"):
        pytest.skip("rsync is required to exercise the sync path")
    synced = _synced_paths(tmp_path / "probe")

    missing = [path for path in _tracked(*RUNTIME_TREES) if path not in synced]

    assert not missing, (
        "runtime assets the installer would not ship. Either the new file belongs "
        "in the installed tree and an exclude pattern is too broad -- anchor it with a "
        f"leading '/' -- or it does not, and does not belong under {RUNTIME_TREES}:\n  " + "\n  ".join(missing)
    )


def _effective_auth_enabled(install: Path) -> bool:
    """Resolve auth the way the systemd unit does: env file over TOML."""
    env_path = install / "config/astinuslab.env"
    env_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    env = {}
    for line in env_lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    if "ASTINUSLAB_AUTH_ENABLED" in env:
        return env["ASTINUSLAB_AUTH_ENABLED"].lower() in {"1", "true", "yes"}
    toml_text = (install / "astinuslab.toml").read_text(encoding="utf-8")
    match = re.search(r"(?ms)^\[auth\].*?^\s*enabled\s*=\s*(\w+)", toml_text)
    return bool(match and match.group(1).lower() == "true")


def test_installed_auth_is_not_silently_disabled_by_the_launcher_env(tmp_path):
    """The installer enables auth in astinuslab.toml and prints a token, but the
    launcher env file it seeds carried ASTINUSLAB_AUTH_ENABLED=false - and the
    systemd unit sources that file, so environment beat TOML and every installed
    release served unauthenticated while claiming otherwise."""
    install = tmp_path / "install"
    (install / "config").mkdir(parents=True)
    (install / "config/astinuslab.env").write_text(
        "ASTINUSLAB_API_PORT=8000\nASTINUSLAB_AUTH_ENABLED=false\n", encoding="utf-8"
    )
    (install / "astinuslab.toml").write_text("[auth]\nenabled = false\n", encoding="utf-8")

    result = run_bash(
        f"source {INSTALL}; INSTALL_DIR={install}; SERVICE_USER=$(id -un); "
        f"GENERATE_ADMIN_TOKEN=false; ADMIN_TOKEN_HASH=''; ALLOW_UNAUTHENTICATED_LOCAL=false; "
        f"configure_admin_auth"
    )

    assert result.returncode == 0, result.stderr
    assert "enabled = true" in (install / "astinuslab.toml").read_text(encoding="utf-8")
    assert _effective_auth_enabled(install), (
        "the launcher env still disables auth that the installer just enabled: "
        + (install / "config/astinuslab.env").read_text(encoding="utf-8")
    )


def test_explicit_no_auth_install_stays_disabled_end_to_end(tmp_path):
    """The escape hatch must still work, and for the same reason: one source of truth."""
    install = tmp_path / "install"
    (install / "config").mkdir(parents=True)
    (install / "config/astinuslab.env").write_text("ASTINUSLAB_AUTH_ENABLED=false\n", encoding="utf-8")
    (install / "astinuslab.toml").write_text("[auth]\nenabled = false\n", encoding="utf-8")

    result = run_bash(
        f"source {INSTALL}; INSTALL_DIR={install}; SERVICE_USER=$(id -un); "
        f"GENERATE_ADMIN_TOKEN=false; ADMIN_TOKEN_HASH=''; ALLOW_UNAUTHENTICATED_LOCAL=true; "
        f"AUTH_ALLOW_UNAUTHENTICATED_LOCAL=true; configure_admin_auth"
    )

    assert result.returncode == 0, result.stderr
    assert not _effective_auth_enabled(install)


def test_package_custody_rollback_refusal_precedes_identity_and_policy_restore(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    harness = f"""
    source {INSTALL}
    MIGRATION_SNAPSHOT={shlex.quote(str(snapshot))}
    quiesce_services() {{ echo quiesced; }}
    systemctl() {{ return 1; }}
    rollback_package_custody() {{ echo package-reconciliation-required; return 1; }}
    rollback_snapshot_host_access() {{ echo forbidden-privileged-restore; }}
    userdel() {{ echo forbidden-identity-delete; }}
    if migration_rollback; then exit 99; fi
    """
    result = run_bash(harness)
    assert result.returncode == 0, result.stderr
    assert "quiesced" in result.stdout
    assert "package-reconciliation-required" in result.stdout
    assert "forbidden" not in result.stdout


def test_package_custody_and_database_subdirectory_have_separate_responsibility():
    text = INSTALL.read_text()
    configure = text[text.index("configure_split_storage_permissions()") : text.index("configure_split_permissions()")]
    assert "prepare_shared_databases" not in configure
    assert 'secure_package_storage_permissions "$root"' in configure
    ensure = text[text.index("ensure_split_storage_root()") : text.index("install_transitional_sudoers()")]
    assert "publish_runtime_identities" in ensure
    migration = text[text.index("migrate_to_split_users()") : text.index("rollback_existing_split_upgrade()")]
    assert (
        migration.index("migration_snapshot_mutable_state")
        < migration.index("ensure_split_storage_root")
        < migration.index("control_services")
    )
    upgrade = text[text.index("prepare_split_upgrade_mutable_state()") : text.index("create_venv()")]
    assert "configure_split_permissions" in upgrade


@pytest.mark.parametrize("retention_fails", [False, True])
def test_split_rollback_retains_candidate_before_schema_restore(tmp_path, retention_fails):
    log = tmp_path / "order"
    harness = f"""
    source {INSTALL}
    PREVIOUS_RELEASE_DIR={tmp_path}
    fail_phase() {{ :; }}
    quiesce_services() {{ echo quiesce >> {log}; }}
    split_database_layout() {{ echo "$1" >> {log}; [[ {str(retention_fails).lower()} != true ]]; }}
    release_rollback() {{ echo schema >> {log}; }}
    migration_rollback() {{ echo layout-and-identities >> {log}; }}
    verify_split_services() {{ echo readiness >> {log}; }}
    if rollback_existing_split_upgrade test; then
      [[ {str(retention_fails).lower()} != true ]]
    else
      [[ {str(retention_fails).lower()} == true ]]
    fi
    """
    result = run_bash(harness)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == (
        ["quiesce", "retain"]
        if retention_fails
        else ["quiesce", "retain", "schema", "layout-and-identities", "readiness"]
    )


@pytest.mark.parametrize("publication_fails", [False, True])
def test_split_commit_does_not_rollback_after_durable_receipt(tmp_path, publication_fails):
    log = tmp_path / "order"
    harness = f"""
    source {INSTALL}
    split_database_layout() {{
      echo "$1" >> {log}
      [[ "$1" != commit || {str(publication_fails).lower()} != true ]]
    }}
    publish_runtime_identities() {{ echo marker >> {log}; }}
    migration_discard_snapshot() {{ echo discard >> {log}; return 17; }}
    migration_commit
    """
    result = run_bash(harness)
    assert result.returncode == 0, result.stderr
    expected = ["retire", "marker", "commit"]
    if publication_fails:
        expected.append("committed")
    expected.append("discard")
    assert log.read_text().splitlines() == expected


def test_split_database_hooks_preserve_endpoint_and_rollback_order():
    source = INSTALL.read_text()
    migrate = source.split("migrate_to_split_users() {", 1)[1].split("\n}\n", 1)[0]
    assert migrate.index("configure_split_permissions") < migrate.index("split_database_layout prepare")
    assert migrate.index("split_database_layout prepare") < migrate.index("control_services")
    rollback = source.split("migration_rollback() {", 1)[1].split("\n}\n", 1)[0]
    assert rollback.index("split_database_layout rollback") < rollback.index("restore_managed_config_state")
    assert (
        rollback.index("rollback_service_logs")
        < rollback.index("restore_acl_snapshot")
        < rollback.index('userdel "$user"')
    )
    storage = source.split("configure_split_storage_permissions() {", 1)[1].split("\n}\n", 1)[0]
    assert "prepare_shared_databases" not in storage
    assert "provision_service_logs" in storage
    main = source.split("main() {", 1)[1]
    assert main.index("split_database_layout pending") < main.index("bootstrap_split_candidate_if_missing")
