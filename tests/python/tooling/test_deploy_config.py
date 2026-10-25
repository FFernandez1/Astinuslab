from __future__ import annotations

import importlib.util
import shlex
from pathlib import Path

ROOT = Path(__file__).parents[3]
SYSTEMD_DIR = ROOT / "ops" / "systemd"


def _logical_systemd_values(path: Path, key: str) -> list[str]:
    values: list[str] = []
    pending: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if pending is not None:
            stripped = line.rstrip("\\").strip()
            pending = f"{pending} {stripped}"
        elif line.startswith(f"{key}="):
            pending = line.split("=", 1)[1].rstrip("\\").strip()
        else:
            continue

        if raw_line.rstrip().endswith("\\"):
            continue
        values.append(pending)
        pending = None

    # Source units are templates; install.sh supplies these values at render
    # time. Use default install values here so command-shape assertions inspect
    # the real ExecStart construct rather than requiring stale literal paths.
    return [
        value.replace("@@CURRENT_DIR@@", "/opt/astinuslab/current")
        .replace("@@INSTALL_DIR@@", "/opt/astinuslab")
        .replace("@@STORAGE_ROOT@@", "/opt/astinuslab/storage")
        for value in values
    ]


def test_systemd_python_module_targets_exist():
    for service in SYSTEMD_DIR.glob("astinuslab-*.service"):
        for key in ("ExecStartPre", "ExecStart"):
            for command in _logical_systemd_values(service, key):
                parts = shlex.split(command)
                if "-m" not in parts:
                    continue
                module = parts[parts.index("-m") + 1]
                assert importlib.util.find_spec(module), f"{service.name} {key} missing module {module}"


def test_systemd_celery_app_targets_resolve_to_a_celery_app():
    """The two worker units launch with `-A`, not `-m`.

    test_systemd_python_module_targets_exist keys on `-m` and so skips both of
    them, covering one unit of three. A `-A` target that merely IMPORTS is not
    enough: Celery's find_app falls back to scanning module globals, so
    `-A astinuslab.jobs.celery_worker` resolves and starts a worker registering
    none of the playground tasks. Assert the target exposes `app` and that the
    playground tasks are on it.
    """
    seen = 0
    for service in sorted(SYSTEMD_DIR.glob("astinuslab-*.service")):
        for command in _logical_systemd_values(service, "ExecStart"):
            parts = shlex.split(command)
            if "-A" not in parts:
                continue
            seen += 1
            target = parts[parts.index("-A") + 1]
            assert importlib.util.find_spec(target), f"{service.name} -A names a missing module {target}"

            module = importlib.import_module(target)
            app = getattr(module, "app", None)
            assert app is not None, f"{service.name} -A target {target} exposes no `app` attribute"
            assert {"playground.spawn_vm", "run_vol3_analysis"} <= set(app.tasks), (
                f"{service.name} -A target {target} registers no playground tasks"
            )

    assert seen == 2, f"expected both worker units to launch with -A, found {seen}"


def test_systemd_runner_starts_celery_worker():
    commands = _logical_systemd_values(SYSTEMD_DIR / "astinuslab-runner.service", "ExecStart")

    assert commands == [
        "/opt/astinuslab/current/.venv/bin/celery -A astinuslab.jobs.celery_main worker "
        "--loglevel=INFO --concurrency=1 --pool=threads "
        "--beat --schedule=/opt/astinuslab/storage/tmp/celerybeat-schedule"
    ]


def test_systemd_analysis_worker_consumes_only_analysis_queue():
    commands = _logical_systemd_values(SYSTEMD_DIR / "astinuslab-analysis.service", "ExecStart")

    assert commands == [
        "/opt/astinuslab/current/.venv/bin/celery -A astinuslab.jobs.celery_main worker "
        "-Q analysis -n analysis@%%h --loglevel=INFO --concurrency=1 --pool=threads"
    ]


def test_systemd_runner_uses_private_tmp():
    # /tmp leaks job metadata (serial logs) to any local user without PrivateTmp.
    unit = (SYSTEMD_DIR / "astinuslab-runner.service").read_text(encoding="utf-8")
    assert "PrivateTmp=true" in unit


def test_dev_launcher_starts_celery_worker_with_concurrency_one():
    text = (ROOT / "astinuslab.sh").read_text(encoding="utf-8")
    assert "--concurrency=1" in text
    assert "--concurrency=4" not in text


def test_dev_launcher_starts_analysis_worker():
    text = (ROOT / "astinuslab.sh").read_text(encoding="utf-8")
    assert "-Q analysis" in text
    assert "-n analysis@%h" in text


def test_install_script_maps_commands_to_debian_packages():
    install_script = (ROOT / "ops" / "deploy" / "install.sh").read_text(encoding="utf-8")

    expected_mappings = {
        "virsh": "libvirt-clients",
        "qemu-img": "qemu-utils",
        "cloud-localds": "cloud-image-utils",
        "pkg-config": "pkg-config",
        "libvirt-dev": "libvirt-dev",
        "rsync": "rsync",
        "ssh-keygen": "openssh-client",
    }
    for command, packages in expected_mappings.items():
        assert f'["{command}"]="{packages}"' in install_script
    assert "pkg-config --exists libvirt" in install_script


def test_install_script_manages_analysis_unit():
    """The analysis worker is managed wherever the other two are.

    Asserted as a comparison rather than a fixed count: the installer grows new
    places that handle all three units together, and a hard-coded number breaks
    on every one of them without saying anything about the invariant.
    """
    install_script = (ROOT / "ops" / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "astinuslab-analysis" in install_script
    templated = [
        install_script.count(f"astinuslab-{name}@${{SERVICE_USER}}.service") for name in ("api", "runner", "analysis")
    ]
    assert len(set(templated)) == 1, f"units handled unevenly: api/runner/analysis = {templated}"
    assert templated[0] >= 4


def test_default_vol3_fetch_timeout_allows_large_debug_packages():
    from astinuslab.core.config import Settings

    assert Settings().vol3_fetch_timeout_seconds == 1800


def test_image_inspection_settings_defaults():
    from astinuslab.core.config import load_settings

    s = load_settings()
    assert s.image_inspection_enabled is True
    assert s.image_inspection_timeout_seconds == 60
    assert s.image_inspection_virt_inspector_bin == "virt-inspector"


def test_uninstall_script_manages_analysis_unit():
    uninstall_script = (ROOT / "ops" / "deploy" / "uninstall.sh").read_text(encoding="utf-8")
    # stop/disable loop references the instance unit for this service user
    assert "astinuslab-analysis@${SERVICE_USER}.service" in uninstall_script
    # the removal loop deletes the template unit file installed by install.sh
    assert '"$SYSTEMD_UNIT_DIR/astinuslab-analysis@.service"' in uninstall_script
    # the running-instances scan is aware of analysis instances too
    assert "astinuslab-analysis@*.service" in uninstall_script


def test_only_api_unit_runs_migrations():
    api = (SYSTEMD_DIR / "astinuslab-api.service").read_text(encoding="utf-8")
    runner = (SYSTEMD_DIR / "astinuslab-runner.service").read_text(encoding="utf-8")
    analysis = (SYSTEMD_DIR / "astinuslab-analysis.service").read_text(encoding="utf-8")
    assert "dbcli init" in api, "API unit must own DB migrations"
    assert "dbcli init" not in runner, "runner must not run migrations (race on SQLite)"
    assert "dbcli init" not in analysis, "analysis must not run migrations (race on SQLite)"


def test_worker_units_start_after_api():
    for name in ("astinuslab-runner.service", "astinuslab-analysis.service"):
        unit = (SYSTEMD_DIR / name).read_text(encoding="utf-8")
        after = next(line for line in unit.splitlines() if line.startswith("After="))
        assert "astinuslab-api@%i.service" in after, f"{name} must start After API"


def test_example_toml_documents_max_concurrent_vms():
    text = (ROOT / "astinuslab.toml.example").read_text(encoding="utf-8")
    assert "max_concurrent_vms" in text, "example config must document the VM cap"


def test_worker_units_do_not_block_sudo_helpers():
    # Workers invoke privileged helpers via `sudo -n` (boot_staging.py,
    # process_support.py preflight); NoNewPrivileges makes every sudo fail,
    # so the release runner would crash-loop on startup.
    for name in ("astinuslab-runner.service", "astinuslab-analysis.service"):
        unit = (SYSTEMD_DIR / name).read_text(encoding="utf-8")
        assert "NoNewPrivileges=true" not in unit, f"{name} must not set NoNewPrivileges (blocks sudo helpers)"


def test_only_runner_allows_netlink_for_privileged_firewall_helpers():
    # sudo inherits the unit's socket-family filter. nft iptables needs
    # AF_NETLINK even when the helper runs as root, including read-only checks.
    base_families = {"AF_INET", "AF_INET6", "AF_UNIX"}
    for name in ("astinuslab-api.service", "astinuslab-runner.service", "astinuslab-analysis.service"):
        declarations = _logical_systemd_values(SYSTEMD_DIR / name, "RestrictAddressFamilies")
        assert len(declarations) == 1
        expected = base_families | ({"AF_NETLINK"} if name == "astinuslab-runner.service" else set())
        assert set(declarations[0].split()) == expected, name


def test_runner_unit_whitelists_libvirt_images_dir():
    # ProtectSystem=strict applies to the sudo-root helper children too; the
    # storage helper writes VM overlays under /var/lib/libvirt/images. Only the
    # runner creates VMs, so only the runner needs that path.
    unit = (SYSTEMD_DIR / "astinuslab-runner.service").read_text(encoding="utf-8")
    rw_lines = [line for line in unit.splitlines() if line.startswith("ReadWritePaths=")]
    assert rw_lines, "runner must keep ReadWritePaths"
    assert any("/var/lib/libvirt/images" in line for line in rw_lines), (
        "runner must whitelist /var/lib/libvirt/images under ProtectSystem=strict"
    )


def test_read_write_paths_are_scoped_per_service():
    """Each identity gets only the mutable paths its own code writes.

    The API owns image staging (`ui/image_routes.py`), catalog edits
    (`ui/jobs_routes.py`), and manual ISF publication. The runner owns VM overlays and ISF builds. The
    analysis worker consumes only the `analysis` queue, whose single task
    touches `storage_dir` and `vol3_symbols_path` and nothing else.
    """
    matrix = {
        "astinuslab-api.service": [
            "/opt/astinuslab/storage",
            "/opt/astinuslab/config",
            "/opt/astinuslab/images/definitions",
            "/opt/astinuslab/vol3/symbols",
        ],
        "astinuslab-runner.service": [
            "/opt/astinuslab/storage",
            "/opt/astinuslab/images/definitions",
            "/opt/astinuslab/vol3/symbols",
            "/var/lib/libvirt/images",
        ],
        "astinuslab-analysis.service": [
            "/opt/astinuslab/storage",
            "/opt/astinuslab/vol3/symbols",
        ],
    }
    for name, expected in matrix.items():
        values = _logical_systemd_values(SYSTEMD_DIR / name, "ReadWritePaths")
        assert len(values) == 1, f"{name} must declare exactly one ReadWritePaths"
        assert values[0].split() == expected, name


def test_units_carry_kernel_and_privilege_hardening():
    # Verified on a disposable host: none of these imply NoNewPrivileges, so
    # the workers keep their `sudo -n` helper access. Adding NoNewPrivileges to
    # a worker remains a stop condition.
    expected = (
        "LockPersonality=true",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectKernelLogs=true",
        "ProtectControlGroups=true",
    )
    for name in ("astinuslab-api.service", "astinuslab-runner.service", "astinuslab-analysis.service"):
        unit = (SYSTEMD_DIR / name).read_text(encoding="utf-8")
        for directive in expected:
            assert directive in unit, f"{name} is missing {directive}"


def test_units_never_restrict_suid_sgid_over_setgid_storage():
    """RestrictSUIDSGID is incompatible with the shared-group storage model.

    Its seccomp filter rejects any chmod whose mode carries setuid or setgid,
    even when the bit is already present and merely being preserved. The
    storage tree is deliberately setgid so the service group is inherited by
    SQLite WAL/SHM siblings and evidence directories, and
    `host_helpers._drop_write_bits` re-applies the existing mode when it clears
    the group write bit. On a disposable host this turned every memory capture
    into `PermissionError: [Errno 1] Operation not permitted` on
    `storage/playground`. Re-adding this directive breaks capture.
    """
    for name in ("astinuslab-api.service", "astinuslab-runner.service", "astinuslab-analysis.service"):
        unit = (SYSTEMD_DIR / name).read_text(encoding="utf-8")
        assert "RestrictSUIDSGID" not in unit, name


def test_all_units_template_protect_home():
    # install.sh picks read-only or tmpfs from the configured storage root, so
    # a custom root under /home stays writable through ReadWritePaths.
    for name in ("astinuslab-api.service", "astinuslab-runner.service", "astinuslab-analysis.service"):
        unit = (SYSTEMD_DIR / name).read_text(encoding="utf-8")
        assert "@@PROTECT_HOME@@" in unit, name


def test_analysis_unit_never_reaches_virtualization_paths():
    unit = (SYSTEMD_DIR / "astinuslab-analysis.service").read_text(encoding="utf-8")
    rw_lines = [line for line in unit.splitlines() if line.startswith("ReadWritePaths=")]
    assert rw_lines
    for line in rw_lines:
        assert "/var/lib/libvirt/images" not in line
        assert "images/definitions" not in line


def test_all_units_make_install_root_read_only_with_templated_exceptions():
    for name in ("astinuslab-api.service", "astinuslab-runner.service", "astinuslab-analysis.service"):
        unit = (SYSTEMD_DIR / name).read_text(encoding="utf-8")
        assert "ReadOnlyPaths=@@INSTALL_DIR@@" in unit
        assert "ReadWritePaths=@@STORAGE_ROOT@@" in unit
        assert "UMask=0007" in unit


def test_only_api_has_no_new_privileges():
    api = (SYSTEMD_DIR / "astinuslab-api.service").read_text(encoding="utf-8")
    assert "NoNewPrivileges=true" in api
    for name in ("astinuslab-runner.service", "astinuslab-analysis.service"):
        unit = (SYSTEMD_DIR / name).read_text(encoding="utf-8")
        assert "NoNewPrivileges=true" not in unit


def test_all_units_load_launcher_env_file():
    # config/astinuslab.env.example documents that ALL systemd units load the
    # env file; without it, overrides like ASTINUSLAB_LIBVIRT_URI reach the
    # API but not the workers that actually use them.
    for name in ("astinuslab-api.service", "astinuslab-runner.service", "astinuslab-analysis.service"):
        unit = (SYSTEMD_DIR / name).read_text(encoding="utf-8")
        assert "EnvironmentFile=-@@INSTALL_DIR@@/config/astinuslab.env" in unit, name


def test_install_script_seeds_configs_from_tracked_examples():
    # Fresh clones only contain *.example files (git ls-files); seeding from
    # live files leaves a fresh release install without astinuslab.toml,
    # profiles.yaml, and network_nodes.yaml.
    install_script = (ROOT / "ops" / "deploy" / "install.sh").read_text(encoding="utf-8")
    for tracked_source in (
        '"$REPO_DIR/astinuslab.toml.example"',
        '"$REPO_DIR/config/catalog/profiles.example.yaml"',
        '"$REPO_DIR/config/catalog/network_nodes.example.yaml"',
    ):
        assert tracked_source in install_script, f"install.sh must seed from {tracked_source}"
    for untracked_source in (
        'install_config_if_missing "$REPO_DIR/astinuslab.toml"',
        'install_config_if_missing "$REPO_DIR/config/catalog/profiles.yaml"',
        'install_config_if_missing "$REPO_DIR/config/catalog/network_nodes.yaml"',
    ):
        assert untracked_source not in install_script, f"install.sh must not seed from untracked {untracked_source}"


def test_dev_setup_preflights_libvirt_headers():
    # ./astinuslab.sh setup must fail fast with a package hint instead of
    # letting pip die inside the libvirt-python C build (fresh-box #1 failure).
    text = (ROOT / "astinuslab.sh").read_text(encoding="utf-8")
    assert "pkg-config --exists libvirt" in text
    assert "sys.version_info >= (3, 12)" in text


def test_uninstall_never_deletes_regular_login_accounts():
    # The recommended install uses --service-user "$USER"; uninstall must not
    # offer to userdel a human account (uid >= 1000).
    uninstall_script = (ROOT / "ops" / "deploy" / "uninstall.sh").read_text(encoding="utf-8")
    assert 'service_uid="$(id -u "$SERVICE_USER")"' in uninstall_script
    assert "service_uid >= 1000" in uninstall_script


def test_uninstall_reverses_selinux_fcontext():
    # install.sh adds a persistent fcontext when SELinux is Enforcing;
    # uninstall must remove it like it removes the AppArmor rule. Both now run
    # the same revocation, so the check belongs to the module that holds it.
    uninstall_script = (ROOT / "ops" / "deploy" / "uninstall.sh").read_text(encoding="utf-8")
    host_access = (ROOT / "ops" / "deploy" / "host_access.py").read_text(encoding="utf-8")
    assert "revoke-all mac" in uninstall_script
    assert '["semanage", "fcontext", "-d", value]' in host_access
