from __future__ import annotations

import grp
import importlib.util
import json
import os
import pwd
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.host import fix_libvirt_path_acl as helper

ROOT = Path(__file__).resolve().parents[3]
_POLICY_SPEC = importlib.util.spec_from_file_location("task4_acl_policy", ROOT / "ops/host/acl_policy.py")
assert _POLICY_SPEC and _POLICY_SPEC.loader
_POLICY = importlib.util.module_from_spec(_POLICY_SPEC)
_POLICY_SPEC.loader.exec_module(_POLICY)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _require_acl_tools() -> None:
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("ACL tools are not available")
    try:
        pwd.getpwnam("libvirt-qemu")
    except KeyError:
        pytest.skip("libvirt-qemu user is not available")


def _pick_non_owner_user() -> str:
    owner = pwd.getpwuid(os.getuid()).pw_name
    for entry in pwd.getpwall():
        if entry.pw_name not in {owner, "root"}:
            return entry.pw_name
    pytest.skip("No secondary user available for ACL regression test")


def test_fix_libvirt_path_acl_rejects_extra_argument(tmp_path):
    helper = _repo_root() / "ops" / "host" / "fix_libvirt_path_acl.sh"
    result = subprocess.run([str(helper), str(tmp_path), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 2


def test_fix_libvirt_path_acl_requires_absolute_target(tmp_path):
    helper = _repo_root() / "ops" / "host" / "fix_libvirt_path_acl.sh"
    result = subprocess.run([str(helper), "relative-target"], capture_output=True, text=True)
    assert result.returncode == 2


# Task 4 policy contract tests.
def test_task4_wrapper_rejects_extra_argument_and_has_no_root_override():
    helper = (_repo_root() / "ops" / "host" / "fix_libvirt_path_acl.sh").read_text(encoding="utf-8")
    assert "[[ $# -ne 1" in helper
    assert '"$1" != "--probe"' in helper
    assert '"$1" != /*' in helper
    assert "${2:-" not in helper
    assert "STORAGE_ROOT" not in helper


def test_acl_helper_probe_validates_caller_without_touching_a_path(monkeypatch):
    monkeypatch.setenv("SUDO_USER", "astinuslab-runner")
    monkeypatch.setattr(
        helper,
        "load_policy",
        lambda _path: {
            "allowed_callers": ["astinuslab-runner"],
            "allowed_roots": ["/not-opened-by-probe"],
            "storage_groups": [],
            "evidence_read_groups": ["astinuslab"],
        },
    )
    monkeypatch.setattr(helper, "apply_acl", lambda *_args, **_kwargs: pytest.fail("probe mutated ACLs"))

    assert helper.main(["--probe"]) == 0


def test_policy_keeps_storage_validation_and_evidence_read_groups_separate(monkeypatch, tmp_path):
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    policy_path = tmp_path / "acl-policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": _POLICY.SCHEMA_VERSION,
                "allowed_callers": ["astinuslab-runner"],
                "allowed_roots": [str(storage_root)],
                "storage_groups": ["storage-writers"],
                "evidence_read_groups": ["astinuslab"],
            }
        ),
        encoding="utf-8",
    )
    policy_path.chmod(0o644)
    original_lstat = Path.lstat

    def trusted_policy_lstat(path: Path):
        info = original_lstat(path)
        if path == policy_path:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0)
        return info

    monkeypatch.setattr(Path, "lstat", trusted_policy_lstat)
    monkeypatch.setattr(_POLICY, "normalize_roots", lambda roots: roots)

    policy = _POLICY.load_policy(policy_path)

    assert policy["storage_groups"] == ["storage-writers"]
    assert policy["evidence_read_groups"] == ["astinuslab"]


def test_acl_helper_uses_storage_groups_only_for_write_validation(monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setenv("SUDO_USER", "astinuslab-runner")
    monkeypatch.setattr(
        helper,
        "load_policy",
        lambda _path: {
            "allowed_callers": ["astinuslab-runner"],
            "allowed_roots": ["/srv/astinuslab/storage"],
            "storage_groups": ["storage-writers"],
            "evidence_read_groups": ["astinuslab"],
        },
    )
    monkeypatch.setattr(helper, "_validate_raw_target", lambda _value: None)
    monkeypatch.setattr(helper, "select_root", lambda _target, _roots: Path("/srv/astinuslab/storage"))
    monkeypatch.setattr(helper, "_policy_gids", lambda names: seen.setdefault("gid_names", names) or frozenset())

    def fake_apply(_target, _root, _caller, allowed_gids, evidence_read_groups):
        seen["allowed_gids"] = allowed_gids
        seen["evidence_read_groups"] = evidence_read_groups

    monkeypatch.setattr(helper, "apply_acl", fake_apply)

    assert helper.main(["/srv/astinuslab/storage/dumps/job-1"]) == 0
    assert seen["gid_names"] == ["storage-writers"]
    assert seen["evidence_read_groups"] == ("astinuslab",)


@pytest.mark.parametrize("prefix, allow_subdirectory", _POLICY.ROOT_REJECTION_TABLE)
def test_task4_each_root_rejection_table_row(prefix, allow_subdirectory):
    assert _POLICY.reject_broad_root(prefix) is not None
    descendant = (
        "/home/operator/storage"
        if prefix == "/home"
        else "/var/lib/astinuslab/storage"
        if prefix == "/var"
        else f"{prefix.rstrip('*')}/dedicated"
    )
    if allow_subdirectory:
        assert _POLICY.reject_broad_root(descendant) is None
    else:
        assert _POLICY.reject_broad_root(descendant) is not None


def test_task4_prefix_table_has_explicit_home_and_var_exceptions():
    source = (_repo_root() / "ops" / "host" / "acl_policy.py").read_text(encoding="utf-8")
    assert "ROOT_REJECTION_TABLE" in source
    assert '("/home", True)' in source
    assert '("/var", True)' in source
    assert '("/lib*", False)' in source


@pytest.mark.parametrize("broad_root", ["/opt", "/srv"])
def test_policy_rejects_exact_broad_application_roots(broad_root):
    with pytest.raises(ValueError, match="not a registrable storage directory"):
        _POLICY.validate_root(broad_root)


@pytest.mark.parametrize(
    "root",
    [
        "/srv/astinuslab evidence",
        "/srv/astinuslab:evidence",
        '/srv/astinuslab"evidence',
        "/srv/astinuslab\\evidence",
        "/srv/astinuslab\nevidence",
    ],
)
def test_policy_rejects_storage_paths_that_cannot_be_safely_rendered(root):
    with pytest.raises(ValueError, match="unsupported characters"):
        _POLICY.validate_root(root)


def test_storage_root_for_install_allows_only_default_or_disjoint_paths(monkeypatch, tmp_path):
    install = tmp_path / "install"
    default_storage = install / "storage"
    nested_storage = install / "nested-storage"
    ancestor_storage = tmp_path
    disjoint_storage = tmp_path / "disjoint-storage"
    for path in (default_storage, nested_storage, disjoint_storage):
        path.mkdir(parents=True)
    monkeypatch.setattr(_POLICY, "validate_root_syntax", lambda path: Path(path))
    monkeypatch.setattr(_POLICY, "validate_root", lambda path: Path(path))
    monkeypatch.setattr(_POLICY, "validate_trusted_parent_chain", lambda path: None)

    assert _POLICY.validate_storage_root_for_install(default_storage, install) == default_storage
    assert _POLICY.validate_storage_root_for_install(disjoint_storage, install) == disjoint_storage
    with pytest.raises(ValueError, match="path-disjoint"):
        _POLICY.validate_storage_root_for_install(nested_storage, install)
    with pytest.raises(ValueError, match="path-disjoint"):
        _POLICY.validate_storage_root_for_install(ancestor_storage, install)


def test_nonexistent_default_storage_still_requires_render_safe_syntax():
    install = Path("/opt/astinuslab unsafe")

    with pytest.raises(ValueError, match="unsupported characters"):
        _POLICY.validate_storage_root_for_install(install / "storage", install)


def test_custom_storage_root_rejects_a_service_replaceable_parent(monkeypatch):
    root = Path("/srv/astinuslab-acceptance/storage")
    modes = {
        "/": SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0),
        "/srv": SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0),
        "/srv/astinuslab-acceptance": SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_uid=1000),
    }

    monkeypatch.setattr(Path, "lstat", lambda path: modes[str(path)])

    with pytest.raises(ValueError, match="parent chain must be root-owned"):
        _POLICY.validate_trusted_parent_chain(root)


def test_custom_storage_root_accepts_a_root_owned_stable_parent_chain(monkeypatch):
    root = Path("/srv/astinuslab-acceptance/storage")
    modes = {
        "/": SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0),
        "/srv": SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0),
        "/srv/astinuslab-acceptance": SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_uid=0),
    }

    monkeypatch.setattr(Path, "lstat", lambda path: modes[str(path)])

    _POLICY.validate_trusted_parent_chain(root)


def test_task5_rejects_malformed_relative_segments_before_open():
    for value, message in [
        ("", "empty"),
        (".", "dot"),
        ("..", "parent"),
        ("/absolute", "absolute"),
        ("has/slash", "slash"),
        ("has\x00nul", "NUL"),
    ]:
        with pytest.raises(ValueError, match=message):
            helper._validate_segment(value)


def test_task5_walk_rejects_symlinked_intermediate_component(tmp_path):
    _require_acl_tools()
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        helper.apply_acl(root / "link" / "target", root, pwd.getpwuid(os.getuid()).pw_name)
    assert not (outside / "target").exists()


def test_task5_rejects_symlinked_direct_child(tmp_path):
    _require_acl_tools()
    root = tmp_path / "root"
    target = root / "capture"
    outside = tmp_path / "outside"
    target.mkdir(parents=True)
    outside.write_bytes(b"private")
    (target / "dump.bin").symlink_to(outside)

    with pytest.raises(OSError):
        helper.apply_acl(target, root, pwd.getpwuid(os.getuid()).pw_name)


@pytest.mark.parametrize("invalid_component", ["root", "path", "child-directory", "child-file"])
def test_acl_validation_failure_closes_all_opened_descriptors(monkeypatch, tmp_path, invalid_component):
    root = tmp_path / "root"
    target = root / "capture"
    target.mkdir(parents=True)
    child = target / "child"
    if invalid_component == "child-directory":
        child.mkdir()
    else:
        child.write_bytes(b"fixture")
    for directory in (root, target):
        directory.chmod(0o700)
    invalid = {"root": root, "path": target}.get(invalid_component, child)
    invalid.chmod(0o702)
    caller = pwd.getpwuid(os.getuid()).pw_name
    original_open = os.open
    opened = []

    def record_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(helper.os, "open", record_open)
    monkeypatch.setattr(helper, "_acl_text", lambda _fd: "group::---\n")
    monkeypatch.setattr(helper, "_apply_fd_acl", lambda *_args, **_kwargs: None)
    try:
        with pytest.raises(OSError, match="world writable"):
            helper.apply_acl(target, root, caller)
        assert opened
        for descriptor in opened:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(descriptor)
    finally:
        # Keep the failing pre-fix probe from leaking into other tests.
        for descriptor in opened:
            helper._close_quietly(descriptor)


def test_acl_child_probe_cannot_block_on_a_fifo(monkeypatch, tmp_path):
    _require_acl_tools()
    root = tmp_path / "root"
    target = root / "capture"
    target.mkdir(parents=True)
    root.chmod(0o700)
    target.chmod(0o700)
    os.mkfifo(target / "service-planted-fifo")
    caller = pwd.getpwuid(os.getuid()).pw_name
    original_open = os.open

    def guarded_open(path, flags, *args, **kwargs):
        if path == "service-planted-fifo":
            assert flags & os.O_NONBLOCK
            raise OSError("test stopped before opening FIFO")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(helper.os, "open", guarded_open)
    monkeypatch.setattr(helper, "_apply_fd_acl", lambda *_args, **_kwargs: None)

    with pytest.raises(OSError, match="test stopped"):
        helper.apply_acl(target, root, caller)


def test_acl_helper_rejects_hardlinked_qcow2_child_without_exposing_victim(tmp_path):
    _require_acl_tools()
    root = tmp_path / "storage"
    target = root / "playground/disks/job-1"
    target.mkdir(parents=True)
    victim = tmp_path / "astinuslab.db"
    victim.write_bytes(b"database")
    os.link(victim, target / "leak.qcow2")
    for directory in (root, root / "playground", root / "playground/disks", target):
        directory.chmod(0o700)

    with pytest.raises(OSError, match="multiply-linked"):
        helper.apply_acl(target, root, pwd.getpwuid(os.getuid()).pw_name)

    victim_acl = subprocess.run(["getfacl", "-cp", victim], check=True, capture_output=True, text=True).stdout
    assert "user:libvirt-qemu:" not in victim_acl


def test_task5_rejects_unexpected_named_acl_before_widening_mask(tmp_path):
    _require_acl_tools()
    other_user = _pick_non_owner_user()
    root = tmp_path / "root"
    target = root / "capture"
    target.mkdir(parents=True)
    root.chmod(0o700)
    target.chmod(0o700)
    subprocess.run(
        ["setfacl", "-m", f"u:{other_user}:r--", "--", str(target)],
        check=True,
    )

    with pytest.raises(ValueError, match="unexpected named ACL"):
        helper.apply_acl(target, root, pwd.getpwuid(os.getuid()).pw_name)


def test_task5_mask_fixture_keeps_effective_permissions(tmp_path):
    _require_acl_tools()
    owner = pwd.getpwuid(os.getuid()).pw_name
    root = tmp_path / "root"
    target = root / "nested" / "capture"
    target.mkdir(parents=True)
    (target / "dump.bin").write_bytes(b"dump")
    for directory in (root, root / "nested", target):
        subprocess.run(
            ["setfacl", "-m", "g::---,m::--x", "--", str(directory)],
            check=True,
        )

    helper.apply_acl(target, root, owner)
    output = subprocess.run(
        ["getfacl", "-p", "--absolute-names", str(root), str(root / "nested"), str(target)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "user:libvirt-qemu:r-x" in output
    blocks = output.split("\n\n")
    ancestor_blocks = [
        block for block in blocks if f"# file: {root}\n" in block or f"# file: {root / 'nested'}\n" in block
    ]
    target_block = next(block for block in blocks if str(target) in block)
    assert all("user:libvirt-qemu:--x" in block for block in ancestor_blocks)
    assert "user:libvirt-qemu:r-x" in target_block
    assert "mask::r-x" in target_block


def test_effective_verification_rejects_mask_clamped_entries():
    """The post-setfacl check must compare effective permissions, not just spot
    the "#effective:" annotation: treating the annotation's presence as success
    accepts the silent mask clamp the check exists to catch."""
    entries = {"libvirt-qemu": "r-x"}

    helper._verify_effective("user:libvirt-qemu:r-x", entries)

    for clamped in (
        "user:libvirt-qemu:r-x\t#effective:--x",
        "user:libvirt-qemu:r-x\t#effective:---",
        "",
    ):
        with pytest.raises(OSError, match="not applied effectively"):
            helper._verify_effective(clamped, entries)


def _dir_with_mode(tmp_path: Path, name: str, mode: int) -> Path:
    directory = tmp_path / name
    directory.mkdir()
    os.chmod(directory, mode)
    return directory


def test_component_check_allows_group_write_only_for_a_policy_named_group(tmp_path):
    """The supported installed layout is storage at 2770 so the service group can
    create SQLite WAL/SHM siblings. Rejecting group write outright would deny
    every capture there, so the root-owned policy names which group is tolerated
    - and no other group, and never world write."""
    _require_acl_tools()
    installed = _dir_with_mode(tmp_path, "installed", 0o2770)
    own_gid = os.stat(installed).st_gid

    fd = os.open(installed, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        helper._check_directory(fd, os.fstat(fd).st_uid, frozenset({own_gid}))

        with pytest.raises(OSError, match="group writable"):
            helper._check_directory(fd, os.fstat(fd).st_uid, frozenset())

        with pytest.raises(OSError, match="group writable"):
            helper._check_directory(fd, os.fstat(fd).st_uid, frozenset({own_gid + 4242}))
    finally:
        os.close(fd)


def test_component_check_never_tolerates_world_write(tmp_path):
    world = _dir_with_mode(tmp_path, "world", 0o2777)
    fd = os.open(world, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(OSError, match="world writable"):
            helper._check_directory(fd, os.fstat(fd).st_uid, frozenset({os.stat(world).st_gid}))
    finally:
        os.close(fd)


def test_component_check_allows_root_or_authorized_caller_owned_descendants(monkeypatch):
    caller_owned = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=2000, st_gid=2000)
    monkeypatch.setattr(helper.os, "fstat", lambda _fd: caller_owned)
    monkeypatch.setattr(helper, "_acl_text", lambda _fd: "group::---\n")

    helper._check_directory(9, 1000, allowed_uids=frozenset({2000}))

    root_owned = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0, st_gid=0)
    monkeypatch.setattr(helper.os, "fstat", lambda _fd: root_owned)
    helper._check_directory(9, 1000, allowed_uids=frozenset({2000, 0}))

    monkeypatch.setattr(helper.os, "fstat", lambda _fd: caller_owned)
    with pytest.raises(OSError, match="owner mismatch"):
        helper._check_directory(9, 1000, allowed_uids=frozenset({3000}))


def test_acl_walk_trusts_root_uid_and_authorized_caller_for_descendants(monkeypatch, tmp_path):
    _require_acl_tools()
    root = tmp_path / "storage"
    target = root / "dumps"
    target.mkdir(parents=True)
    root.chmod(0o700)
    target.chmod(0o700)
    caller = pwd.getpwuid(os.getuid()).pw_name
    observed: list[frozenset[int]] = []
    original = helper._check_directory

    def capture(fd, expected_uid, allowed_gids=frozenset(), allowed_uids=frozenset()):
        observed.append(allowed_uids)
        return original(fd, expected_uid, allowed_gids, allowed_uids)

    monkeypatch.setattr(helper, "_check_directory", capture)
    monkeypatch.setattr(helper, "_apply_fd_acl", lambda *_args, **_kwargs: None)

    helper.apply_acl(target, root, caller)

    assert frozenset({0, os.getuid()}) in observed


def test_policy_gids_rejects_a_group_this_host_does_not_have():
    assert helper._policy_gids([]) == frozenset()
    with pytest.raises(ValueError, match="unknown group"):
        helper._policy_gids(["astinuslab-group-that-does-not-exist"])


def test_mask_preserves_the_existing_group_class(tmp_path):
    """The mask shares storage with the group permission bits, so building it
    from the named entries alone rewrites them: the supported 2770 storage
    layout would silently drop to 2710 and the service group would lose the
    write access SQLite needs for WAL/SHM siblings."""
    _require_acl_tools()
    root = _dir_with_mode(tmp_path, "storage", 0o2770)
    target = root / "job-001"
    target.mkdir()
    os.chmod(target, 0o2770)
    gid = os.stat(root).st_gid

    helper.apply_acl(target, root, pwd.getpwuid(os.getuid()).pw_name, frozenset({gid}))

    assert os.stat(root).st_mode & 0o7777 == 0o2770
    assert os.stat(target).st_mode & 0o7777 == 0o2770


def test_mask_stays_tight_when_the_group_class_is_empty(tmp_path):
    """Conversely, a checkout-style root with no group access must not gain any:
    the union gives back exactly the entries this run grants."""
    assert helper._mask_for({"libvirt-qemu": "x"}, "") == "x"
    assert helper._mask_for({"libvirt-qemu": "r-x"}, "") == "rx"
    assert helper._mask_for({"libvirt-qemu": "x"}, "rwx") == "rwx"


def test_split_capture_acl_grants_shared_group_effective_read(monkeypatch, tmp_path):
    """The API has a different UID from the runner and reads through astinuslab.

    The ACL operation therefore has to grant the shared group on both capture
    directories and artifacts, and include that grant in the explicit mask.
    """
    target = tmp_path / "capture"
    target.mkdir()
    target.chmod(0o700)
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    commands: list[list[str]] = []
    verified: list[dict[str, str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(helper, "_reject_unexpected_named_entries", lambda *_args: None)
    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    monkeypatch.setattr(helper, "_acl_text", lambda _fd: "")
    monkeypatch.setattr(helper, "_verify_effective", lambda _text, entries: verified.append(entries))
    try:
        helper._apply_fd_acl(
            fd,
            caller=pwd.getpwuid(os.getuid()).pw_name,
            target=True,
            directory=True,
            shared_groups=("astinuslab",),
        )
    finally:
        os.close(fd)

    mutation = next(command for command in commands if command[:2] == ["setfacl", "-m"])
    assert "g:astinuslab:r-x" in mutation[2]
    assert "m::rx" in mutation[2]
    assert verified == [{"user:libvirt-qemu": "r-x", "group:astinuslab": "r-x"}]


def test_existing_caller_permissions_read_the_acl_group_class():
    acl = "\n".join(
        (
            "user::rwx",
            "user:libvirt-qemu:--x",
            "group::rwx",
            "group:astinuslab:--x",
            "mask::rwx",
            "other::---",
        )
    )
    shared_gid = os.stat(__file__).st_gid

    member = helper._existing_caller_permissions(acl, "astinuslab-runner", frozenset({shared_gid}), shared_gid)
    stranger = helper._existing_caller_permissions(acl, "astinuslab-runner", frozenset(), shared_gid)
    clamped = helper._existing_caller_permissions(
        acl.replace("mask::rwx", "mask::r-x"), "astinuslab-runner", frozenset({shared_gid}), shared_gid
    )

    assert member == "rwx"
    assert stranger == "---"
    assert clamped == "r-x"


def test_caller_traversal_grant_never_reduces_access_the_caller_already_has(monkeypatch, tmp_path):
    """A named user entry outranks the group entries for that UID.

    The shared storage root is owned by the API identity and group-writable so
    the runner can create SQLite WAL/SHM siblings. Granting the runner a bare
    traversal entry there replaces its group rwx with --x, and the broker turns
    into a read-only database mid-capture.
    """
    target = tmp_path / "storage"
    target.mkdir()
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    caller = _pick_non_owner_user()
    shared_gid = os.fstat(fd).st_gid
    commands: list[list[str]] = []
    verified: list[dict[str, str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(helper, "_reject_unexpected_named_entries", lambda *_args: None)
    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    monkeypatch.setattr(
        helper,
        "_acl_text",
        lambda _fd: "user::rwx\ngroup::rwx\nmask::rwx\nother::---\n",
    )
    monkeypatch.setattr(helper, "_caller_gids", lambda _caller: frozenset({shared_gid}))
    monkeypatch.setattr(helper, "_verify_effective", lambda _text, entries: verified.append(entries))
    try:
        helper._apply_fd_acl(fd, caller=caller, target=False, directory=True, traverse_only=True)
    finally:
        os.close(fd)

    mutation = next(command for command in commands if command[:2] == ["setfacl", "-m"])
    assert f"u:{caller}:rwx" in mutation[2]
    assert verified == [{"user:libvirt-qemu": "x", f"user:{caller}": "rwx"}]


def test_caller_traversal_grant_stays_narrow_without_existing_group_access(monkeypatch, tmp_path):
    target = tmp_path / "component"
    target.mkdir()
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    caller = _pick_non_owner_user()
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(helper, "_reject_unexpected_named_entries", lambda *_args: None)
    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    monkeypatch.setattr(helper, "_acl_text", lambda _fd: "user::rwx\ngroup::---\nother::---\n")
    monkeypatch.setattr(helper, "_caller_gids", lambda _caller: frozenset())
    monkeypatch.setattr(helper, "_verify_effective", lambda _text, _entries: None)
    try:
        helper._apply_fd_acl(fd, caller=caller, target=False, directory=True, traverse_only=True)
    finally:
        os.close(fd)

    mutation = next(command for command in commands if command[:2] == ["setfacl", "-m"])
    assert f"u:{caller}:--x" in mutation[2]
    assert f"u:{caller}:rwx" not in mutation[2]


def test_named_acl_allowlist_distinguishes_users_from_groups(monkeypatch):
    monkeypatch.setattr(helper, "_acl_text", lambda _fd: "group:astinuslab:r-x\n")
    helper._reject_unexpected_named_entries(9, {("group", "astinuslab")})

    monkeypatch.setattr(helper, "_acl_text", lambda _fd: "user:astinuslab:r-x\n")
    with pytest.raises(ValueError, match="unexpected named ACL"):
        helper._reject_unexpected_named_entries(9, {("group", "astinuslab")})


def test_preserved_evidence_group_must_remain_traverse_only(monkeypatch, tmp_path):
    target = tmp_path / "playground"
    target.mkdir()
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    monkeypatch.setattr(helper, "_acl_text", lambda _fd: "group:astinuslab:r-x\n")
    try:
        with pytest.raises(ValueError, match="permissions"):
            helper._apply_fd_acl(
                fd,
                caller=pwd.getpwuid(os.getuid()).pw_name,
                target=False,
                directory=True,
                traverse_only=True,
                preserved_groups=("astinuslab",),
            )
    finally:
        os.close(fd)


def test_capture_then_live_disk_preserves_approved_group_ancestor_without_regranting_it(monkeypatch, tmp_path):
    """A capture grants group traversal on playground ancestors. A later disk
    preparation must accept that approved entry, but must not grant the shared
    group any new access on the live per-job disk directory itself.
    """
    target = tmp_path / "playground"
    target.mkdir()
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(helper, "_acl_text", lambda _fd: "group:astinuslab:--x\n")
    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    monkeypatch.setattr(helper, "_verify_effective", lambda *_args: None)
    try:
        helper._apply_fd_acl(
            fd,
            caller=pwd.getpwuid(os.getuid()).pw_name,
            target=False,
            directory=True,
            traverse_only=True,
            shared_groups=("astinuslab",),
            preserved_groups=("astinuslab",),
        )
        helper._apply_fd_acl(
            fd,
            caller=pwd.getpwuid(os.getuid()).pw_name,
            target=True,
            directory=True,
            preserved_groups=("astinuslab",),
            libvirt_write=True,
        )
    finally:
        os.close(fd)

    mutations = [command[2] for command in commands if command[:2] == ["setfacl", "-m"]]
    assert "g:astinuslab:x" in mutations[0]
    assert "g:astinuslab:" not in mutations[1]


def test_capture_then_new_live_disk_sequence_keeps_shared_group_off_disk_data(tmp_path):
    _require_acl_tools()
    try:
        pwd.getpwnam("libvirt-qemu")
        grp.getgrnam("astinuslab")
    except KeyError:
        pytest.skip("split-user ACL identities are not available")

    owner = pwd.getpwuid(os.getuid()).pw_name
    root = tmp_path / "storage"
    capture = root / "playground" / "captures" / "capture-1"
    disk = root / "playground" / "disks" / "vm-1"
    capture.mkdir(parents=True)
    disk.mkdir(parents=True)
    (capture / "memory.dump").write_bytes(b"capture")
    overlay = disk / "target.qcow2"
    seed = disk / "seed.iso"
    overlay.write_bytes(b"overlay")
    seed.write_bytes(b"seed")
    for path in (root, root / "playground", capture.parent, capture, disk.parent, disk):
        path.chmod(0o700)
    for path in (capture / "memory.dump", overlay, seed):
        path.chmod(0o600)

    helper.apply_acl(capture, root, owner, evidence_read_groups=("astinuslab",))
    helper.apply_acl(disk, root, owner, evidence_read_groups=("astinuslab",))

    def acl(path: Path) -> str:
        return subprocess.run(
            ["getfacl", "-cp", "--", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    assert "group:astinuslab:--x" in acl(root)
    assert "group:astinuslab:--x" in acl(root / "playground")
    for path in (disk.parent, disk, overlay, seed):
        assert "group:astinuslab:" not in acl(path)
    assert "user:libvirt-qemu:rwx" in acl(disk)
    assert "user:libvirt-qemu:rw-" in acl(overlay)
    assert "user:libvirt-qemu:r--" in acl(seed)


def test_playground_overlay_acl_write_mode_is_narrowly_path_scoped():
    assert helper._libvirt_write_target(("playground", "disks", "job-123"))
    assert not helper._libvirt_write_target(("playground", "captures", "job-123"))
    assert not helper._libvirt_write_target(("ssh_keys",))
    assert not helper._libvirt_write_target(("playground", "disks"))
    assert not helper._libvirt_write_target(("playground", "disks", "job-123", "nested"))
    assert helper._libvirt_write_child("target.qcow2")
    assert not helper._libvirt_write_child("seed.iso")
    assert not helper._libvirt_write_child("user-data")


def test_playground_acl_can_prepare_a_second_vm_in_the_same_session(tmp_path):
    _require_acl_tools()
    root = tmp_path / "storage"
    target = root / "playground" / "disks" / "session-1"
    target.mkdir(parents=True)
    for directory in (root, root / "playground", target.parent, target):
        directory.chmod(0o700)
    first = target / "first.qcow2"
    first.write_bytes(b"first overlay")
    first.chmod(0o600)
    caller = pwd.getpwuid(os.getuid()).pw_name

    helper.apply_acl(target, root, caller)
    second = target / "second.qcow2"
    second.write_bytes(b"second overlay")
    second.chmod(0o600)
    helper.apply_acl(target, root, caller)

    for path, permissions in ((target, "rwx"), (first, "rw-"), (second, "rw-")):
        acl = subprocess.run(["getfacl", "-cp", path], check=True, capture_output=True, text=True).stdout
        assert f"user:libvirt-qemu:{permissions}" in acl
        assert "group::---" in acl
        assert "other::---" in acl


def test_directory_check_rejects_group_write_hidden_by_acl_mask(tmp_path):
    _require_acl_tools()
    target = _dir_with_mode(tmp_path, "masked-group", 0o700)
    subprocess.run(["setfacl", "-m", "g::rwx,m::r-x", target], check=True)
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError, match="group writable"):
            helper._check_directory(fd, os.getuid())
    finally:
        os.close(fd)


@pytest.mark.parametrize("acl", ["", "group::invalid\n", "default:group::---\n"])
def test_directory_check_fails_closed_without_valid_owning_group_acl(monkeypatch, tmp_path, acl):
    target = _dir_with_mode(tmp_path, "invalid-acl", 0o700)
    monkeypatch.setattr(helper, "_acl_text", lambda _fd: acl)
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError, match="cannot determine ACL owning group permissions"):
            helper._check_directory(fd, os.getuid())
    finally:
        os.close(fd)


@pytest.mark.parametrize("mask", ["rwx", "r-x"])
@pytest.mark.parametrize("on_ancestor", [True, False])
def test_playground_acl_rejects_unapproved_named_write_before_changing_mask(tmp_path, mask, on_ancestor):
    _require_acl_tools()
    root = tmp_path / "storage"
    target = root / "playground" / "disks" / "session-1"
    target.mkdir(parents=True)
    for directory in (root, root / "playground", target.parent, target):
        directory.chmod(0o700)
    untrusted = root / "playground" if on_ancestor else target
    other_user = _pick_non_owner_user()
    subprocess.run(["setfacl", "-m", f"u:{other_user}:rwx,m::{mask}", untrusted], check=True)
    before = subprocess.run(["getfacl", "-cp", untrusted], check=True, capture_output=True, text=True).stdout

    with pytest.raises(ValueError, match="unexpected named ACL"):
        helper.apply_acl(target, root, pwd.getpwuid(os.getuid()).pw_name)

    after = subprocess.run(["getfacl", "-cp", untrusted], check=True, capture_output=True, text=True).stdout
    assert after == before


@pytest.mark.parametrize("name", ["first.qcow2", "first-seed.iso", "seed.iso"])
def test_playground_acl_accepts_libvirt_relabelled_regular_disks(monkeypatch, tmp_path, name):
    _require_acl_tools()
    root = tmp_path / "storage"
    target = root / "playground" / "disks" / "session-1"
    target.mkdir(parents=True)
    for directory in (root, root / "playground", target.parent, target):
        directory.chmod(0o700)
    artifact = target / name
    artifact.write_bytes(b"existing VM disk")
    artifact.chmod(0o600)
    inode = artifact.stat().st_ino
    qemu_uid = pwd.getpwnam("libvirt-qemu").pw_uid
    original_fstat = os.fstat

    def relabelled_fstat(fd):
        info = original_fstat(fd)
        if info.st_ino == inode:
            fields = list(info)
            fields[4] = qemu_uid
            return os.stat_result(fields)
        return info

    # Only emulate libvirt's root-only chown; exercise real files and ACL tools.
    monkeypatch.setattr(helper.os, "fstat", relabelled_fstat)
    caller = pwd.getpwuid(os.getuid()).pw_name
    helper.apply_acl(target, root, caller)
    helper.apply_acl(target, root, caller)

    acl = subprocess.run(["getfacl", "-cp", artifact], check=True, capture_output=True, text=True).stdout
    expected = "rw-" if name == "first.qcow2" else "r--"
    assert f"user:libvirt-qemu:{expected}" in acl
    assert "group::---" in acl


@pytest.mark.parametrize(
    "relative_target,name,is_directory",
    [
        ("dumps/session-1", "first.qcow2", False),
        ("playground/disks/session-1", "private.bin", False),
        ("playground/disks/session-1", "unrelated.iso", False),
        ("playground/disks/session-1", "first.qcow2", True),
    ],
)
def test_playground_acl_rejects_libvirt_ownership_outside_regular_vm_disks(
    monkeypatch, tmp_path, relative_target, name, is_directory
):
    _require_acl_tools()
    root = tmp_path / "storage"
    target = root / relative_target
    target.mkdir(parents=True)
    artifact = target / name
    if is_directory:
        artifact.mkdir()
    else:
        artifact.write_bytes(b"private")
    for directory in (root, target, *target.parents):
        if directory == root or root in directory.parents:
            directory.chmod(0o700)
    artifact.chmod(0o700 if is_directory else 0o600)
    inode = artifact.stat().st_ino
    qemu_uid = pwd.getpwnam("libvirt-qemu").pw_uid
    original_fstat = os.fstat

    def relabelled_fstat(fd):
        info = original_fstat(fd)
        if info.st_ino == inode:
            fields = list(info)
            fields[4] = qemu_uid
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(helper.os, "fstat", relabelled_fstat)
    with pytest.raises(OSError, match="owner mismatch"):
        helper.apply_acl(target, root, pwd.getpwuid(os.getuid()).pw_name)


def test_shared_api_read_acl_is_limited_to_evidence_paths():
    assert helper._shared_read_target(("dumps", "job-123"))
    assert helper._shared_read_target(("dumps", "job-123", "captures"))
    assert helper._shared_read_target(("playground", "captures", "job-123"))
    assert not helper._shared_read_target(("ssh_keys",))
    assert not helper._shared_read_target(("astinuslab.db",))
    assert not helper._shared_read_target(("workload-cache", "ubuntu", "lamp"))
    assert not helper._shared_read_target(("playground", "disks", "job-123"))


def test_playground_overlay_acl_grants_only_qemu_write_on_directory_and_file(monkeypatch, tmp_path):
    target = tmp_path / "job-123"
    target.mkdir()
    overlay = target / "target.qcow2"
    overlay.write_bytes(b"overlay")
    directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    overlay_fd = os.open(overlay, os.O_RDONLY | os.O_NOFOLLOW)
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(helper, "_reject_unexpected_named_entries", lambda *_args: None)
    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    monkeypatch.setattr(helper, "_acl_text", lambda _fd: "")
    monkeypatch.setattr(helper, "_verify_effective", lambda *_args: None)
    try:
        helper._apply_fd_acl(
            directory_fd,
            caller=pwd.getpwuid(os.getuid()).pw_name,
            target=True,
            directory=True,
            shared_groups=("astinuslab",),
            libvirt_write=True,
        )
        helper._apply_fd_acl(
            overlay_fd,
            caller=pwd.getpwuid(os.getuid()).pw_name,
            target=False,
            directory=False,
            shared_groups=("astinuslab",),
            libvirt_write=True,
        )
    finally:
        os.close(overlay_fd)
        os.close(directory_fd)

    mutations = [command[2] for command in commands if command[:2] == ["setfacl", "-m"]]
    assert "u:libvirt-qemu:rwx" in mutations[0]
    assert "u:libvirt-qemu:rw-" in mutations[1]
    assert all("g:astinuslab:" not in mutation for mutation in mutations)
