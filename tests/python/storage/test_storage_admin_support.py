from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_destroy_lab_vms_and_disks_invokes_storage_helper_with_sudo_n(monkeypatch):
    from astinuslab.storage import storage_admin_support as support

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:5] == ["virsh", "-c", "qemu:///system", "list", "--all"]:
            return SimpleNamespace(returncode=0, stdout="astinuslab-job-123\nother\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(support.subprocess, "run", fake_run)

    result = support.destroy_lab_vms_and_disks(
        SimpleNamespace(
            libvirt_uri="qemu:///system",
            libvirt_image_base_dir=Path("/var/lib/libvirt/images/astinuslab"),
        )
    )

    assert result["ok"] is True
    assert [
        "sudo",
        "-n",
        "/usr/local/libexec/astinuslab/libvirt_storage_admin.sh",
        "clear_storage_root",
    ] in calls


def test_destroy_lab_vms_and_disks_times_out_listing_vms(monkeypatch):
    from astinuslab.storage import storage_admin_support as support

    def fake_run(cmd, **kwargs):
        assert kwargs["timeout"] == support.STORAGE_ADMIN_SUBPROCESS_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(support.subprocess, "run", fake_run)

    result = support.destroy_lab_vms_and_disks(SimpleNamespace(libvirt_uri="qemu:///system"))

    assert result == {
        "action": "destroy_vms",
        "ok": False,
        "message": "Timed out while running virsh list.",
    }


def test_reset_libvirt_network_times_out(monkeypatch, tmp_path):
    from astinuslab.storage import storage_admin_support as support

    def fake_run(cmd, **kwargs):
        assert kwargs["timeout"] == support.STORAGE_ADMIN_SUBPROCESS_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(support.subprocess, "run", fake_run)
    # Stub the host boundary before exercising unlink; fake libvirt does not
    # redirect these filesystem paths and CI must never touch host DHCP state.
    files = {f"/var/lib/libvirt/dnsmasq/default.{suffix}": tmp_path / suffix for suffix in ("leases", "status")}
    for path in files.values():
        path.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(support, "Path", files.__getitem__)

    result = support.reset_libvirt_network(SimpleNamespace(libvirt_uri="qemu:///system", libvirt_network="default"))

    assert result == {
        "action": "reset_network",
        "ok": False,
        "message": "Timed out while running virsh net-destroy.",
    }
    assert all(not path.exists() for path in files.values())


def test_purge_tmp_orphans_deletes_removable_entry_under_tmp_root(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    tmp_root = tmp_path / "storage" / "tmp"
    tmp_root.mkdir(parents=True)
    removable = tmp_root / "orphan-upload"
    removable.write_text("stale", encoding="utf-8")

    result = support.purge_tmp_orphans(
        SimpleNamespace(storage_dir=tmp_path / "storage"),
        lambda: [{"name": "orphan-upload", "removable": True}],
    )

    assert result["ok"] is True
    assert result["deleted"] == 1
    assert not removable.exists()


def test_purge_tmp_orphans_refuses_path_traversal_outside_tmp_root(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    tmp_root = tmp_path / "storage" / "tmp"
    tmp_root.mkdir(parents=True)
    outside = tmp_path / "storage" / "outside.tmp"
    outside.write_text("keep", encoding="utf-8")

    result = support.purge_tmp_orphans(
        SimpleNamespace(storage_dir=tmp_path / "storage"),
        lambda: [{"name": "../outside.tmp", "removable": True}],
    )

    assert result["ok"] is False
    assert "refusing to delete non-top-level temporary entry" in result["message"]
    assert outside.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("name", ["..", "/"])
def test_purge_tmp_orphans_refuses_special_single_part_names(tmp_path, name: str):
    from astinuslab.storage import storage_admin_support as support

    tmp_root = tmp_path / "storage" / "tmp"
    tmp_root.mkdir(parents=True)

    result = support.purge_tmp_orphans(
        SimpleNamespace(storage_dir=tmp_path / "storage"),
        lambda: [{"name": name, "removable": True}],
    )

    assert result["ok"] is False
    assert "non-top-level temporary entry" in result["message"]


def test_purge_tmp_orphans_skips_non_removable_entries(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    tmp_root = tmp_path / "storage" / "tmp"
    tmp_root.mkdir(parents=True)
    active_tmp = tmp_root / "job-active"
    active_tmp.write_text("keep", encoding="utf-8")

    result = support.purge_tmp_orphans(
        SimpleNamespace(storage_dir=tmp_path / "storage"),
        lambda: [{"name": "job-active", "removable": False}],
    )

    assert result == {
        "action": "purge_tmp",
        "ok": True,
        "message": "Temporary files deleted: 0. Kept for safety: 1.",
        "deleted": 0,
        "skipped": 1,
    }
    assert active_tmp.read_text(encoding="utf-8") == "keep"


def test_purge_tmp_orphans_unlinks_alias_without_deleting_live_job_target(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    tmp_root = tmp_path / "storage" / "tmp"
    live_job = tmp_root / "job-active"
    live_job.mkdir(parents=True)
    marker = live_job / "active.marker"
    marker.write_text("keep", encoding="utf-8")
    alias = tmp_root / "orphan-alias"
    alias.symlink_to(live_job, target_is_directory=True)

    result = support.purge_tmp_orphans(
        SimpleNamespace(storage_dir=tmp_path / "storage"),
        lambda: [
            {"name": alias.name, "removable": True},
            {"name": live_job.name, "removable": False},
        ],
    )

    assert result["ok"] is True
    assert result["deleted"] == 1
    assert result["skipped"] == 1
    assert not alias.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("target_exists", [True, False], ids=["regular", "broken"])
def test_purge_tmp_orphans_unlinks_external_symlink_without_touching_target(tmp_path, target_exists: bool):
    from astinuslab.storage import storage_admin_support as support

    tmp_root = tmp_path / "storage" / "tmp"
    tmp_root.mkdir(parents=True)
    external = tmp_path / "external-runtime"
    if target_exists:
        external.mkdir()
        marker = external / "keep.marker"
        marker.write_text("keep", encoding="utf-8")
    alias = tmp_root / "external-alias"
    alias.symlink_to(external, target_is_directory=True)

    result = support.purge_tmp_orphans(
        SimpleNamespace(storage_dir=tmp_path / "storage"),
        lambda: [{"name": alias.name, "removable": True}],
    )

    assert result["ok"] is True
    assert result["deleted"] == 1
    assert result["skipped"] == 0
    assert not alias.is_symlink()
    if target_exists:
        assert marker.read_text(encoding="utf-8") == "keep"


def test_purge_tmp_orphans_never_follows_nested_symlinks(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    tmp_root = tmp_path / "storage" / "tmp"
    ordinary = tmp_root / "ordinary"
    ordinary.mkdir(parents=True)
    external = tmp_path / "external-runtime"
    external.mkdir()
    external_marker = external / "keep.marker"
    external_marker.write_text("keep", encoding="utf-8")
    protected = tmp_root / "package-instance-reservation-locks"
    protected.mkdir()
    protected_marker = protected / "active.lock"
    protected_marker.write_bytes(b"")
    (ordinary / "external-alias").symlink_to(external, target_is_directory=True)
    (ordinary / "protected-alias").symlink_to(protected, target_is_directory=True)

    result = support.purge_tmp_orphans(
        SimpleNamespace(storage_dir=tmp_path / "storage"),
        lambda: [{"name": ordinary.name, "removable": True}],
    )

    assert result["ok"] is True
    assert result["deleted"] == 1
    assert not ordinary.exists()
    assert external_marker.read_text(encoding="utf-8") == "keep"
    assert protected_marker.is_file()


@pytest.mark.parametrize(
    "name",
    ["package-instance-locks", "package-instance-reservation-locks"],
)
def test_purge_tmp_orphans_preserves_package_lock_infrastructure_even_if_marked_removable(tmp_path, name: str):
    from astinuslab.storage import storage_admin_support as support

    lock_dir = tmp_path / "storage" / "tmp" / name
    lock_dir.mkdir(parents=True)
    lock_path = lock_dir / "active.lock"
    lock_path.write_bytes(b"")

    result = support.purge_tmp_orphans(
        SimpleNamespace(storage_dir=tmp_path / "storage"),
        lambda: [{"name": name, "removable": True}],
    )

    assert result["deleted"] == 0
    assert result["skipped"] == 1
    assert lock_path.is_file()


@pytest.mark.parametrize(
    "name",
    ["package-instance-locks", "package-instance-reservation-locks"],
)
def test_purge_tmp_orphans_refuses_non_top_level_normalized_lock_names(tmp_path, name: str):
    from astinuslab.storage import storage_admin_support as support

    tmp_root = tmp_path / "storage" / "tmp"
    lock_dir = tmp_root / name
    lock_dir.mkdir(parents=True)
    lock_path = lock_dir / "active.lock"
    lock_path.write_bytes(b"")

    result = support.purge_tmp_orphans(
        SimpleNamespace(storage_dir=tmp_path / "storage"),
        lambda: [{"name": f"alias/../{name}", "removable": True}],
    )

    assert result["ok"] is False
    assert "non-top-level temporary entry" in result["message"]
    assert lock_path.is_file()


@pytest.mark.parametrize(
    "name",
    ["package-instance-locks", "package-instance-reservation-locks"],
)
def test_purge_tmp_orphans_protects_symlink_aliases_of_package_lock_infrastructure(tmp_path, name: str):
    from astinuslab.storage import storage_admin_support as support

    tmp_root = tmp_path / "storage" / "tmp"
    lock_dir = tmp_root / name
    lock_dir.mkdir(parents=True)
    lock_path = lock_dir / "active.lock"
    lock_path.write_bytes(b"")
    (tmp_root / f"{name}-alias").symlink_to(lock_dir, target_is_directory=True)

    result = support.purge_tmp_orphans(
        SimpleNamespace(storage_dir=tmp_path / "storage"),
        lambda: [{"name": f"{name}-alias", "removable": True}],
    )

    assert result["deleted"] == 0
    assert result["skipped"] == 1
    assert lock_path.is_file()


def test_purge_tmp_orphans_reports_missing_tmp_without_creating_it(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    tmp_root = tmp_path / "storage" / "tmp"

    result = support.purge_tmp_orphans(SimpleNamespace(storage_dir=tmp_path / "storage"), lambda: [])

    assert result == {
        "action": "purge_tmp",
        "ok": True,
        "message": "storage/tmp does not exist.",
        "deleted": 0,
        "skipped": 0,
    }
    assert not tmp_root.exists()


def test_clear_storage_state_unlinks_symlink_outside_storage_root(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    storage = tmp_path / "storage"
    storage.mkdir()
    outside = tmp_path / "outside-keys"
    outside.mkdir()
    (outside / "id_rsa").write_text("secret", encoding="utf-8")
    (storage / "ssh_keys").symlink_to(outside, target_is_directory=True)

    result = support.clear_storage_state(SimpleNamespace(storage_dir=storage), lambda: False)

    assert result["ok"] is True
    assert not (storage / "ssh_keys").is_symlink()
    assert (outside / "id_rsa").exists()


def test_clear_runtime_traces_refuses_symlink_outside_storage_root(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    storage = tmp_path / "storage"
    storage.mkdir()
    outside = tmp_path / "outside-runtime"
    outside.mkdir()
    (outside / "transcript.log").write_text("keep", encoding="utf-8")
    (storage / "tmp").symlink_to(outside, target_is_directory=True)

    result = support.clear_runtime_traces(SimpleNamespace(storage_dir=storage), lambda: False)

    assert result["ok"] is False
    assert "unsafe temporary root" in result["message"]
    assert (outside / "transcript.log").exists()


def test_clear_runtime_traces_refuses_symlink_to_directory_inside_storage_root(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    storage = tmp_path / "storage"
    storage.mkdir()
    target = storage / "internal-runtime"
    target.mkdir()
    marker = target / "transcript.log"
    marker.write_text("keep", encoding="utf-8")
    (storage / "tmp").symlink_to(target, target_is_directory=True)

    result = support.clear_runtime_traces(SimpleNamespace(storage_dir=storage), lambda: False)

    assert result["ok"] is False
    assert "unsafe temporary root" in result["message"]
    assert marker.read_text(encoding="utf-8") == "keep"


def test_clear_runtime_traces_refuses_broken_temporary_root_symlink(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    storage = tmp_path / "storage"
    storage.mkdir()
    tmp_root = storage / "tmp"
    tmp_root.symlink_to(storage / "missing-runtime", target_is_directory=True)

    result = support.clear_runtime_traces(SimpleNamespace(storage_dir=storage), lambda: False)

    assert result["ok"] is False
    assert "unsafe temporary root" in result["message"]
    assert tmp_root.is_symlink()


def test_purge_tmp_orphans_refuses_broken_temporary_root_symlink(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    storage = tmp_path / "storage"
    storage.mkdir()
    tmp_root = storage / "tmp"
    tmp_root.symlink_to(storage / "missing-runtime", target_is_directory=True)

    result = support.purge_tmp_orphans(SimpleNamespace(storage_dir=storage), lambda: [])

    assert result["ok"] is False
    assert "unsafe temporary root" in result["message"]
    assert tmp_root.is_symlink()


def test_clear_runtime_traces_removes_an_ordinary_symlink_and_its_target(monkeypatch, tmp_path):
    from astinuslab.storage import storage_admin_support as support

    storage = tmp_path / "storage"
    tmp_root = storage / "tmp"
    target = tmp_root / "z-target"
    target.mkdir(parents=True)
    (target / "trace.bin").write_bytes(b"trace")
    alias = tmp_root / "a-alias"
    alias.symlink_to(target, target_is_directory=True)
    real_iterdir = support.Path.iterdir

    def aliases_first(path):
        entries = real_iterdir(path)
        return iter(sorted(entries)) if path == tmp_root else entries

    monkeypatch.setattr(support.Path, "iterdir", aliases_first)

    result = support.clear_runtime_traces(SimpleNamespace(storage_dir=storage), lambda: False)

    assert result["ok"] is True
    assert not alias.is_symlink()
    assert not target.exists()
    assert tmp_root.is_dir()
    assert list(tmp_root.iterdir()) == []


def test_clear_runtime_traces_removes_a_broken_symlink(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    storage = tmp_path / "storage"
    tmp_root = storage / "tmp"
    tmp_root.mkdir(parents=True)
    alias = tmp_root / "broken-alias"
    alias.symlink_to(tmp_root / "missing-target", target_is_directory=True)

    result = support.clear_runtime_traces(SimpleNamespace(storage_dir=storage), lambda: False)

    assert result["ok"] is True
    assert not alias.is_symlink()
    assert tmp_root.is_dir()
    assert list(tmp_root.iterdir()) == []


def test_destroy_lab_vms_and_disks_reports_virsh_list_failure(monkeypatch):
    from astinuslab.storage import storage_admin_support as support

    def fake_run(cmd, **kwargs):
        assert cmd == ["virsh", "-c", "qemu:///system", "list", "--all", "--name"]
        return SimpleNamespace(returncode=1, stdout="ignored", stderr="libvirt unavailable\n")

    monkeypatch.setattr(support.subprocess, "run", fake_run)

    result = support.destroy_lab_vms_and_disks(SimpleNamespace(libvirt_uri="qemu:///system"))

    assert result == {
        "action": "destroy_vms",
        "ok": False,
        "message": "libvirt unavailable",
    }


def test_destroy_lab_vms_and_disks_falls_back_when_nvram_undefine_fails(monkeypatch):
    from astinuslab.storage import storage_admin_support as support

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:5] == ["virsh", "-c", "qemu:///system", "list", "--all"]:
            return SimpleNamespace(returncode=0, stdout="astinuslab-job-abc\n", stderr="")
        if cmd == ["virsh", "-c", "qemu:///system", "undefine", "astinuslab-job-abc", "--nvram"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="no nvram")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(support.subprocess, "run", fake_run)

    result = support.destroy_lab_vms_and_disks(
        SimpleNamespace(
            libvirt_uri="qemu:///system",
            libvirt_image_base_dir=Path("/var/lib/libvirt/images/astinuslab"),
        )
    )

    assert result["ok"] is True
    assert result["message"] == "Lab VMs/disks removed."
    assert "VM removed: astinuslab-job-abc" in result["details"]
    assert ["virsh", "-c", "qemu:///system", "undefine", "astinuslab-job-abc", "--nvram"] in calls
    assert ["virsh", "-c", "qemu:///system", "undefine", "astinuslab-job-abc"] in calls


def test_reset_libvirt_network_ignores_lease_permission_errors(monkeypatch):
    from astinuslab.storage import storage_admin_support as support

    commands: list[str] = []
    unlinked: list[str] = []

    def fake_unlink(path, **kwargs):
        unlinked.append(str(path))
        raise PermissionError("denied")

    def fake_run(cmd, **kwargs):
        commands.append(cmd[3])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(support.Path, "unlink", fake_unlink)
    monkeypatch.setattr(support.subprocess, "run", fake_run)

    result = support.reset_libvirt_network(SimpleNamespace(libvirt_uri="qemu:///system", libvirt_network="default"))

    assert result == {
        "action": "reset_network",
        "ok": True,
        "message": "libvirt network restarted: default.",
    }
    assert unlinked == ["/var/lib/libvirt/dnsmasq/default.leases", "/var/lib/libvirt/dnsmasq/default.status"]
    assert commands == ["net-destroy", "net-start", "net-autostart"]


def test_clear_runtime_and_storage_state_refuse_active_jobs(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    storage = tmp_path / "storage"
    tmp_dir = storage / "tmp"
    logs_dir = storage / "logs"
    ssh_dir = storage / "ssh_keys"
    tmp_dir.mkdir(parents=True)
    logs_dir.mkdir()
    ssh_dir.mkdir()
    db_path = storage / "astinuslab.db"
    db_path.write_text("db", encoding="utf-8")

    settings = SimpleNamespace(storage_dir=storage)

    runtime_result = support.clear_runtime_traces(settings, lambda: True)
    storage_result = support.clear_storage_state(settings, lambda: True)

    assert runtime_result == {
        "action": "clear_runtime",
        "ok": False,
        "message": "Active jobs exist; runtime traces were not deleted.",
    }
    assert storage_result == {
        "action": "clear_state",
        "ok": False,
        "message": "Active jobs exist; DB and SSH keys were not deleted.",
    }
    assert tmp_dir.exists()
    assert logs_dir.exists()
    assert ssh_dir.exists()
    assert db_path.exists()


def test_storage_admin_cleanup_uses_storage_dir_not_base_dir(tmp_path):
    """When ASTINUSLAB_STORAGE_DIR diverges from base_dir/storage, storage-admin
    cleanup must act on settings.storage_dir and leave base_dir/storage untouched."""
    from astinuslab.storage import storage_admin_support as support

    base_dir = tmp_path / "repo"
    storage_dir = tmp_path / "external-storage"

    # Decoy tree under base_dir/storage that must be left untouched.
    decoy = base_dir / "storage"
    (decoy / "tmp").mkdir(parents=True)
    (decoy / "tmp" / "orphan-upload").write_text("decoy", encoding="utf-8")
    (decoy / "dumps").mkdir()
    (decoy / "ssh_keys").mkdir()
    (decoy / "astinuslab.db").write_text("decoy-db", encoding="utf-8")

    # Real configured storage tree.
    (storage_dir / "tmp").mkdir(parents=True)
    orphan = storage_dir / "tmp" / "orphan-upload"
    orphan.write_text("stale", encoding="utf-8")
    (storage_dir / "logs").mkdir()
    (storage_dir / "dumps").mkdir()
    ssh_keys = storage_dir / "ssh_keys"
    ssh_keys.mkdir()
    db_path = storage_dir / "astinuslab.db"
    db_path.write_text("real-db", encoding="utf-8")

    settings = SimpleNamespace(base_dir=base_dir, data_dir=base_dir, storage_dir=storage_dir)

    purge = support.purge_tmp_orphans(settings, lambda: [{"name": "orphan-upload", "removable": True}])
    runtime = support.clear_runtime_traces(settings, lambda: False)
    state = support.clear_storage_state(settings, lambda: False)

    assert purge["ok"] is True and purge["deleted"] == 1
    assert runtime["ok"] is True
    assert state["ok"] is True

    # Configured storage tree was cleaned.
    assert not orphan.exists()
    assert not (storage_dir / "dumps").exists()
    assert not ssh_keys.exists()
    assert not db_path.exists()
    assert (storage_dir / "logs").exists()  # recreated by clear_runtime_traces

    # Decoy base_dir/storage tree untouched.
    assert (decoy / "tmp" / "orphan-upload").read_text(encoding="utf-8") == "decoy"
    assert (decoy / "ssh_keys").exists()
    assert (decoy / "astinuslab.db").read_text(encoding="utf-8") == "decoy-db"


def test_reset_libvirt_network_refuses_invalid_network_name():
    from astinuslab.storage import storage_admin_support as support

    result = support.reset_libvirt_network(
        SimpleNamespace(libvirt_uri="qemu:///system", libvirt_network="../default"),
    )

    assert result == {
        "action": "reset_network",
        "ok": False,
        "message": "Invalid libvirt network name: ../default",
    }


@pytest.mark.parametrize("relative", ["storage/databases/astinuslab.db", "external/operator.db"])
def test_clear_state_refuses_relocated_database_before_removing_any_state(tmp_path, relative):
    from astinuslab.storage import storage_admin_support as support

    storage = tmp_path / "storage"
    keys = storage / "ssh_keys"
    keys.mkdir(parents=True)
    key = keys / "retained-key"
    key.write_text("private fixture", encoding="utf-8")
    stale = storage / "astinuslab.db"
    stale.write_text("stale flat file", encoding="utf-8")
    database = tmp_path / relative
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_text("configured database", encoding="utf-8")

    result = support.clear_storage_state(SimpleNamespace(storage_dir=storage, db_path=database), lambda: False)

    assert result["ok"] is False
    assert "configured database" in result["message"]
    assert database.read_text(encoding="utf-8") == "configured database"
    assert stale.read_text(encoding="utf-8") == "stale flat file"
    assert key.read_text(encoding="utf-8") == "private fixture"


def test_truncate_active_logs_creates_logs_dir_and_truncates_only_active_logs(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    logs = tmp_path / "storage" / "logs"
    logs.mkdir(parents=True)
    (logs / "api.log").write_text("old api", encoding="utf-8")
    (logs / "runner.log").write_text("old runner", encoding="utf-8")
    (logs / "analysis-worker.log").write_text("old analysis", encoding="utf-8")
    archived = logs / "docs.log"
    archived.write_text("keep", encoding="utf-8")

    result = support.truncate_active_logs(
        SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path, storage_dir=tmp_path / "storage")
    )

    assert result == {
        "action": "truncate_logs",
        "ok": True,
        "message": "Active logs truncated: 3. Running processes will resume writing new lines.",
    }
    assert (logs / "api.log").read_text(encoding="utf-8") == ""
    assert (logs / "runner.log").read_text(encoding="utf-8") == ""
    assert (logs / "analysis-worker.log").read_text(encoding="utf-8") == ""
    assert archived.read_text(encoding="utf-8") == "keep"


def test_truncate_active_logs_covers_every_declared_service_log(tmp_path):
    """The button and the Logs page must agree on which files are 'active'."""
    from astinuslab.core.logging_config import LOG_FILE_NAMES
    from astinuslab.storage import storage_admin_support as support

    support.truncate_active_logs(
        SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path, storage_dir=tmp_path / "storage")
    )

    logs = tmp_path / "storage" / "logs"
    assert sorted(p.name for p in logs.iterdir()) == sorted(LOG_FILE_NAMES)


def test_clear_vol3_symbol_cache_removes_contents_keeps_root(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    symbols = tmp_path / "vol3" / "symbols"
    (symbols / "linux").mkdir(parents=True)
    (symbols / "linux" / "a.json").write_text("{}", encoding="utf-8")
    (symbols / "windows.json").write_text("{}", encoding="utf-8")
    fake = SimpleNamespace(vol3_symbols_path=symbols)

    result = support.clear_vol3_symbol_cache(fake)

    assert result["ok"] is True
    assert result["removed"] == 2
    assert symbols.exists()  # root kept
    assert list(symbols.iterdir()) == []  # contents gone


def test_clear_vol3_symbol_cache_handles_missing_dir(tmp_path):
    from astinuslab.storage import storage_admin_support as support

    fake = SimpleNamespace(vol3_symbols_path=tmp_path / "nope" / "symbols")
    result = support.clear_vol3_symbol_cache(fake)
    assert result == {"action": "clear_symbols", "ok": True, "message": "Symbol cache already empty.", "removed": 0}


_FIXED_ENTRIES = ["logs", "dumps", "api.pid", "runner.pid", "ssh_keys", "astinuslab.db"]
_LOCK_DIRECTORIES = ["package-instance-locks", "package-instance-reservation-locks"]


def _clear_fixed_entry(storage: Path, name: str) -> dict:
    from astinuslab.storage import storage_admin_support as support

    clear = support.clear_storage_state if name in {"ssh_keys", "astinuslab.db"} else support.clear_runtime_traces
    return clear(SimpleNamespace(storage_dir=storage), lambda: False)


@pytest.mark.parametrize("name", _FIXED_ENTRIES)
@pytest.mark.parametrize("lock_name", _LOCK_DIRECTORIES)
def test_fixed_cleanup_preserves_protected_aliases(tmp_path: Path, name: str, lock_name: str):
    storage = tmp_path / "storage"
    lock_dir = storage / "tmp" / lock_name
    lock_dir.mkdir(parents=True)
    lock = lock_dir / "active.lock"
    lock.write_bytes(b"held")
    inode = lock.stat().st_ino
    directory_inode = lock_dir.stat().st_ino
    alias = storage / name
    alias.symlink_to(lock_dir, target_is_directory=True)

    result = _clear_fixed_entry(storage, name)

    assert alias.is_symlink()
    assert lock.read_bytes() == b"held"
    assert lock.stat().st_ino == inode
    assert lock_dir.stat().st_ino == directory_inode
    assert result["ok"] is True
    assert result["fixed_deleted"] == 0
    assert result["fixed_skipped"] == 1
    assert "safety" in result["message"].lower()


@pytest.mark.parametrize("name", _FIXED_ENTRIES)
@pytest.mark.parametrize("target_kind", ["internal", "external", "broken"])
def test_fixed_cleanup_unlinks_alias_without_touching_target(tmp_path: Path, name: str, target_kind: str):
    storage = tmp_path / "storage"
    storage.mkdir()
    target = storage / "ordinary-target" if target_kind == "internal" else tmp_path / "external-target"
    if target_kind != "broken":
        target.mkdir()
        (target / "keep").write_bytes(b"keep")
    alias = storage / name
    alias.symlink_to(target, target_is_directory=True)

    result = _clear_fixed_entry(storage, name)

    assert not alias.is_symlink()
    assert alias.is_dir() if name == "logs" else not alias.exists()
    if target_kind != "broken":
        assert (target / "keep").read_bytes() == b"keep"
    else:
        assert not target.exists()
    assert result["ok"] is True
    assert result["fixed_deleted"] == 1
    assert result["fixed_skipped"] == 0


@pytest.mark.parametrize("name", _FIXED_ENTRIES)
@pytest.mark.parametrize("lock_name", _LOCK_DIRECTORIES)
def test_fixed_cleanup_skips_post_check_directory_swap(monkeypatch, tmp_path: Path, name: str, lock_name: str):
    from astinuslab.storage import storage_admin_support as support

    storage = tmp_path / "storage"
    lock_dir = storage / "tmp" / lock_name
    lock_dir.mkdir(parents=True)
    lock = lock_dir / "active.lock"
    lock.write_bytes(b"held")
    inode = lock.stat().st_ino
    directory_inode = lock_dir.stat().st_ino
    entry = storage / name
    entry.mkdir()
    swapped = False
    checks = 0
    real_is_dir = support.Path.is_dir
    real_lstat = support.os.lstat

    def swap():
        nonlocal swapped
        if not swapped:
            swapped = True
            entry.rmdir()
            entry.symlink_to(lock_dir, target_is_directory=True)

    def observed_is_dir(path):
        result = real_is_dir(path)
        if path == entry and result:
            swap()
        return result

    def observed_lstat(path, *, dir_fd=None):
        nonlocal checks
        result = real_lstat(path, dir_fd=dir_fd)
        if path == name and dir_fd is not None:
            checks += 1
            if checks == 2:
                swap()
        return result

    monkeypatch.setattr(support.Path, "is_dir", observed_is_dir)
    monkeypatch.setattr(support.os, "lstat", observed_lstat)

    result = _clear_fixed_entry(storage, name)

    assert swapped
    assert entry.is_symlink()
    assert lock.read_bytes() == b"held"
    assert lock.stat().st_ino == inode
    assert lock_dir.stat().st_ino == directory_inode
    assert result["ok"] is True
    assert result["fixed_deleted"] == 0
    assert result["fixed_skipped"] == 1


@pytest.mark.parametrize("lock_name", _LOCK_DIRECTORIES)
def test_fixed_cleanup_log_recreation_skips_new_alias(monkeypatch, tmp_path: Path, lock_name: str):
    from astinuslab.storage import storage_admin_support as support

    storage = tmp_path / "storage"
    lock_dir = storage / "tmp" / lock_name
    lock_dir.mkdir(parents=True)
    (lock_dir / "active.lock").write_bytes(b"held")
    logs = storage / "logs"
    logs.mkdir()
    real_mkdir = support.os.mkdir
    swapped = False

    def swap_before_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and (path == logs or path == "logs"):
            swapped = True
            logs.symlink_to(lock_dir, target_is_directory=True)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(support.os, "mkdir", swap_before_mkdir)

    result = _clear_fixed_entry(storage, "logs")

    assert swapped
    assert logs.is_symlink()
    assert list(lock_dir.iterdir()) == [lock_dir / "active.lock"]
    assert result["ok"] is True
    assert "safety" in result["message"].lower()
    assert result["fixed_skipped"] == 1


@pytest.mark.parametrize("name", ["dumps", "ssh_keys"])
def test_fixed_cleanup_reports_real_directory_deletion_error(monkeypatch, tmp_path: Path, name: str):
    from astinuslab.storage import storage_admin_support as support

    storage = tmp_path / "storage"
    entry = storage / name
    entry.mkdir(parents=True)
    (entry / "keep").write_bytes(b"keep")
    real_rmtree = support.shutil.rmtree

    def denied(path, *args, **kwargs):
        if path == entry or path == name:
            if kwargs.get("ignore_errors"):
                return None
            raise PermissionError("cleanup denied")
        return real_rmtree(path, *args, **kwargs)

    setattr(denied, "avoids_symlink_attacks", True)
    monkeypatch.setattr(support.shutil, "rmtree", denied)

    result = _clear_fixed_entry(storage, name)

    assert result["ok"] is False
    assert "cleanup denied" in result["message"]
    assert (entry / "keep").read_bytes() == b"keep"


@pytest.mark.parametrize("target_kind", ["internal", "external", "broken"])
def test_symbol_cleanup_counts_unlinked_aliases_without_following_targets(tmp_path: Path, target_kind: str):
    from astinuslab.storage import storage_admin_support as support

    symbols = tmp_path / "symbols"
    symbols.mkdir()
    target = tmp_path / "target"
    if target_kind == "internal":
        target = symbols / "target"
    if target_kind != "broken":
        target.mkdir()
        (target / "keep").write_bytes(b"keep")
    alias = symbols / "alias"
    alias.symlink_to(target, target_is_directory=True)
    # Internal targets are separately eligible cache entries; fixed-entry tests prove target preservation.
    result = support.clear_vol3_symbol_cache(SimpleNamespace(vol3_symbols_path=symbols))

    assert result["ok"] is True
    assert result["removed"] == (2 if target_kind == "internal" else 1)
    assert not alias.is_symlink()
    if target_kind == "external":
        assert (target / "keep").read_bytes() == b"keep"
