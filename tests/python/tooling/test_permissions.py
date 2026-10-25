"""Contract for `ops/deploy/permissions.py`.

Six installer phases decide who can reach an installation's trees. They run as
root over directories that include an operator's own evidence, so the failure
that matters is not a crash: it is a phase that reports success while leaving a
tree readable by an identity it was supposed to exclude, or while quietly
widening an ACL it was supposed to narrow.

The code was six `python3 - <<'PY'` heredocs in ops/deploy/install.sh until the
extraction, so the only checks that ever reached it were `compile()` and
pyflakes. These cases exercise the module itself; the shell entry points keep
their own coverage in test_install_script.py.

Everything here runs unprivileged: `chown` to the identity that already owns
the fixture is permitted, and `setfacl` works on files the caller owns.
"""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ops.deploy import permissions

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "ops/deploy/permissions.py"
pytestmark = [pytest.mark.unit, pytest.mark.host]

USER = pwd.getpwuid(os.getuid()).pw_name


def _require_acl_tools() -> None:
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("ACL tools are not available")


def _own_group() -> str:
    """A group the caller belongs to, so fchown is permitted unprivileged."""
    return grp.getgrgid(os.getgid()).gr_name


def _tree(base: Path, name: str = "tree") -> Path:
    root = base / name
    (root / "nested").mkdir(parents=True)
    (root / "top.txt").write_bytes(b"top\n")
    (root / "nested/inner.txt").write_bytes(b"inner\n")
    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    root.chmod(0o755)
    return root


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _acl(path: Path) -> str:
    return subprocess.run(["getfacl", "-cp", "--", str(path)], capture_output=True, text=True, check=True).stdout


# --------------------------------------------------------------------------
# tree
# --------------------------------------------------------------------------


def test_tree_applies_the_directory_and_file_modes_recursively(tmp_path):
    root = _tree(tmp_path)

    permissions.secure_tree(str(root), USER, _own_group(), "0750", "0640", [USER])

    assert _mode(root) == 0o750
    assert _mode(root / "nested") == 0o750
    assert _mode(root / "top.txt") == 0o640
    assert _mode(root / "nested/inner.txt") == 0o640


def test_tree_refuses_a_root_owned_by_an_identity_it_does_not_know(tmp_path):
    root = _tree(tmp_path)

    # Only root is allowed, so the fixture's own owner is foreign.
    with pytest.raises(SystemExit) as caught:
        permissions.secure_tree(str(root), USER, _own_group(), "0750", "0640", ["root"])

    assert "refusing mutable-tree root with foreign custody" in str(caught.value)


def test_tree_refuses_a_symlink_planted_in_the_tree(tmp_path):
    root = _tree(tmp_path)
    (root / "nested/link").symlink_to(tmp_path)

    with pytest.raises(SystemExit) as caught:
        permissions.secure_tree(str(root), USER, _own_group(), "0750", "0640", [USER])

    assert "refusing unsupported mutable-tree entry: link" in str(caught.value)


def test_tree_refuses_a_multiply_linked_file(tmp_path):
    root = _tree(tmp_path)
    os.link(root / "top.txt", root / "nested/hardlink.txt")

    with pytest.raises(SystemExit) as caught:
        permissions.secure_tree(str(root), USER, _own_group(), "0750", "0640", [USER])

    assert "non-regular or multiply-linked mutable-tree entry" in str(caught.value)


def test_tree_refuses_a_fifo(tmp_path):
    root = _tree(tmp_path)
    os.mkfifo(root / "nested/pipe")

    with pytest.raises(SystemExit) as caught:
        permissions.secure_tree(str(root), USER, _own_group(), "0750", "0640", [USER])

    assert "non-regular or multiply-linked mutable-tree entry" in str(caught.value)


def test_tree_stops_at_its_depth_limit(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    deep = root / "nested"
    for index in range(4):
        deep = deep / f"d{index}"
    deep.mkdir(parents=True)

    monkeypatch.setattr(permissions.EntryBudget, "enter", _shallow_enter(2))
    with pytest.raises(SystemExit) as caught:
        permissions.secure_tree(str(root), USER, _own_group(), "0750", "0640", [USER])

    assert "mutable tree exceeds depth limit" in str(caught.value)


def _shallow_enter(limit: int):
    def enter(self, depth: int) -> None:
        if depth > limit:
            raise SystemExit(self.depth_message)

    return enter


# --------------------------------------------------------------------------
# EntryBudget
# --------------------------------------------------------------------------


def test_entry_budget_counts_across_the_whole_walk_not_per_directory():
    budget = permissions.EntryBudget(depth=4, entries=2, depth_message="deep", entry_message="wide")

    budget.count()
    budget.count()

    with pytest.raises(SystemExit) as caught:
        budget.count()
    assert "wide" in str(caught.value)


def test_entry_budget_admits_its_own_depth_and_refuses_beyond_it():
    budget = permissions.EntryBudget(depth=2, entries=10, depth_message="deep", entry_message="wide")

    budget.enter(2)

    with pytest.raises(SystemExit) as caught:
        budget.enter(3)
    assert "deep" in str(caught.value)


# --------------------------------------------------------------------------
# resolve_allowed_owners
# --------------------------------------------------------------------------


def test_allowed_owners_always_includes_root_and_skips_identities_that_do_not_exist():
    resolved = permissions.resolve_allowed_owners([USER, "astinuslab-nonexistent-identity", ""])

    assert 0 in resolved
    assert pwd.getpwnam(USER).pw_uid in resolved
    # An installation names identities it may not have created yet, and the
    # shell passes "" for an absent optional owner. Neither is an error.
    assert len(resolved) == 2


# --------------------------------------------------------------------------
# ssh-keys
# --------------------------------------------------------------------------


def test_ssh_keys_grants_the_runner_read_only_and_nothing_to_the_group(tmp_path):
    _require_acl_tools()
    root = _tree(tmp_path, "ssh_keys")

    permissions.secure_ssh_keys(str(root), USER, USER, _own_group())

    # The chmod sets 0o2700, then setfacl writes the mask into the group bits,
    # so the mode reads 0o2750. The group still has nothing: `group::---` in the
    # ACL is the real answer, and the group bits are the mask. Asserting the
    # mode alone would look like a leak, and asserting 0o2700 would be wrong.
    assert _mode(root) == 0o2750
    assert _mode(root / "top.txt") == 0o640
    directory_acl = _acl(root)
    assert f"user:{USER}:r-x" in directory_acl
    assert "group::---" in directory_acl
    assert f"user:{USER}:r--" in _acl(root / "top.txt")


def test_ssh_keys_clears_an_acl_entry_it_did_not_write(tmp_path):
    _require_acl_tools()
    root = _tree(tmp_path, "ssh_keys")
    subprocess.run(["setfacl", "-m", f"u:{USER}:rwx", "--", str(root / "top.txt")], check=True)

    permissions.secure_ssh_keys(str(root), USER, USER, _own_group())

    # -b runs before the grant, so the stale rwx entry cannot survive it.
    assert f"user:{USER}:rwx" not in _acl(root / "top.txt")
    assert f"user:{USER}:r--" in _acl(root / "top.txt")


def test_ssh_keys_refuses_a_runner_account_that_does_not_exist(tmp_path):
    root = _tree(tmp_path, "ssh_keys")

    with pytest.raises(KeyError):
        permissions.secure_ssh_keys(str(root), USER, "astinuslab-nonexistent-identity", _own_group())


def test_ssh_keys_refuses_a_multiply_linked_key(tmp_path):
    _require_acl_tools()
    root = _tree(tmp_path, "ssh_keys")
    os.link(root / "top.txt", root / "nested/hardlink.txt")

    with pytest.raises(SystemExit) as caught:
        permissions.secure_ssh_keys(str(root), USER, USER, _own_group())

    assert "non-regular or multiply-linked entry" in str(caught.value)


# --------------------------------------------------------------------------
# libvirt-images
# --------------------------------------------------------------------------


def test_libvirt_images_grants_qemu_read_access_without_taking_ownership(tmp_path):
    _require_acl_tools()
    root = _tree(tmp_path, "images")
    before = root.lstat().st_uid

    permissions.secure_libvirt_images(str(root), USER, [USER])

    assert root.lstat().st_uid == before
    assert f"user:{USER}:r-x" in _acl(root)
    assert f"user:{USER}:r--" in _acl(root / "top.txt")


def test_libvirt_images_mask_preserves_group_access_the_tree_already_had(tmp_path):
    _require_acl_tools()
    root = _tree(tmp_path, "images")
    (root / "top.txt").chmod(0o660)

    permissions.secure_libvirt_images(str(root), USER, [USER])

    # Group had rw; the grant adds r. A mask of r-- would silently drop the w.
    assert "mask::rw-" in _acl(root / "top.txt")


def test_libvirt_images_refuses_an_entry_owned_by_an_identity_it_does_not_know(tmp_path):
    _require_acl_tools()
    root = _tree(tmp_path, "images")

    with pytest.raises(SystemExit) as caught:
        permissions.secure_libvirt_images(str(root), "root", [])

    assert "foreign custody or crosses a filesystem boundary" in str(caught.value)


def test_libvirt_images_refuses_a_qemu_account_that_does_not_exist(tmp_path):
    root = _tree(tmp_path, "images")

    with pytest.raises(KeyError):
        permissions.secure_libvirt_images(str(root), "astinuslab-nonexistent-identity", [USER])


@pytest.mark.parametrize(
    "raw,expected",
    [("rwx", "rwx"), ("r-x", "r-x"), ("rx", "r-x"), ("r", "r--"), ("", "---"), ("---", "---")],
)
def test_canonical_normalises_however_getfacl_spelled_the_triple(raw, expected):
    assert permissions._canonical(raw) == expected


# The compact spelling is deliberate: the result feeds a set(), so padding
# it to a triple would add nothing the caller reads.
@pytest.mark.parametrize("mode,expected", [(0o750, "rx"), (0o770, "rwx"), (0o700, ""), (0o740, "r")])
def test_group_permissions_reads_only_the_group_bits(mode, expected):
    assert permissions._group_permissions(mode) == expected


# --------------------------------------------------------------------------
# directory-root
# --------------------------------------------------------------------------


def test_directory_root_sets_owner_and_mode_on_the_root_alone(tmp_path):
    root = _tree(tmp_path, "storage")

    permissions.secure_directory_root(str(root), USER, _own_group(), "2750", [USER])

    assert _mode(root) == 0o2750
    # The walk-free phase must not touch what is underneath it.
    assert _mode(root / "nested") == 0o755


def test_directory_root_refuses_foreign_custody(tmp_path):
    root = _tree(tmp_path, "storage")

    with pytest.raises(SystemExit) as caught:
        permissions.secure_directory_root(str(root), USER, _own_group(), "2750", ["root"])

    assert "refusing managed storage root with foreign custody" in str(caught.value)


def test_directory_root_refuses_a_symlinked_root(tmp_path):
    root = _tree(tmp_path, "storage")
    link = tmp_path / "link"
    link.symlink_to(root)

    with pytest.raises(OSError):
        permissions.secure_directory_root(str(link), USER, _own_group(), "2750", [USER])


# --------------------------------------------------------------------------
# managed-file
# --------------------------------------------------------------------------


def test_managed_file_sets_owner_and_mode_on_one_file(tmp_path):
    root = _tree(tmp_path, "install")

    permissions.secure_managed_file(str(root), "nested/inner.txt", USER, _own_group(), "0640")

    assert _mode(root / "nested/inner.txt") == 0o640
    assert _mode(root / "top.txt") == 0o644


@pytest.mark.parametrize("relative", ["../escape", "nested/../../escape", "nested/./inner.txt", "nested//inner.txt"])
def test_managed_file_refuses_a_traversing_relative_path(tmp_path, relative):
    root = _tree(tmp_path, "install")

    with pytest.raises(SystemExit) as caught:
        permissions.secure_managed_file(str(root), relative, USER, _own_group(), "0640")

    assert "unsafe managed-file path" in str(caught.value)


def test_managed_file_refuses_a_multiply_linked_file(tmp_path):
    root = _tree(tmp_path, "install")
    os.link(root / "top.txt", tmp_path / "harvested")

    with pytest.raises(SystemExit) as caught:
        permissions.secure_managed_file(str(root), "top.txt", USER, _own_group(), "0640")

    assert "managed file is non-regular or multiply linked" in str(caught.value)


def test_managed_file_refuses_a_symlinked_target(tmp_path):
    root = _tree(tmp_path, "install")
    (root / "nested/link.txt").symlink_to(root / "top.txt")

    with pytest.raises(OSError):
        permissions.secure_managed_file(str(root), "nested/link.txt", USER, _own_group(), "0640")


# --------------------------------------------------------------------------
# drop-legacy-evidence-acl
# --------------------------------------------------------------------------


def _evidence_tree(base: Path) -> Path:
    root = base / "storage"
    (root / "dumps/case1").mkdir(parents=True)
    (root / "playground/captures/run1").mkdir(parents=True)
    (root / "dumps/case1/mem.raw").write_bytes(b"dump\n")
    (root / "playground/captures/run1/disk.qcow2").write_bytes(b"cap\n")
    for path in root.rglob("*"):
        path.chmod(0o750 if path.is_dir() else 0o640)
    root.chmod(0o750)
    return root


def _shared_group_exists() -> bool:
    try:
        grp.getgrnam(permissions.SHARED_GROUP)
    except KeyError:
        return False
    return True


def test_legacy_evidence_replaces_the_per_user_grant_with_the_shared_group(tmp_path):
    _require_acl_tools()
    if not _shared_group_exists():
        pytest.skip(f"the {permissions.SHARED_GROUP} group is not present on this host")
    root = _evidence_tree(tmp_path)
    legacy = root / "dumps/case1/mem.raw"
    subprocess.run(["setfacl", "-m", f"u:{USER}:rw-", "--", str(legacy)], check=True)

    permissions.drop_legacy_evidence_acls(str(root), USER)

    acl = _acl(legacy)
    assert f"user:{USER}:" not in acl
    assert f"group:{permissions.SHARED_GROUP}:r--" in acl


def test_legacy_evidence_gives_the_root_traverse_only(tmp_path):
    _require_acl_tools()
    if not _shared_group_exists():
        pytest.skip(f"the {permissions.SHARED_GROUP} group is not present on this host")
    root = _evidence_tree(tmp_path)

    permissions.drop_legacy_evidence_acls(str(root), USER)

    # The root is a path to the evidence, not evidence. Read here would widen
    # the grant to everything else the storage root holds.
    assert f"group:{permissions.SHARED_GROUP}:--x" in _acl(root)
    assert f"group:{permissions.SHARED_GROUP}:r-x" in _acl(root / "dumps")


def test_legacy_evidence_tolerates_an_evidence_tree_that_does_not_exist(tmp_path):
    _require_acl_tools()
    if not _shared_group_exists():
        pytest.skip(f"the {permissions.SHARED_GROUP} group is not present on this host")
    root = tmp_path / "storage"
    (root / "dumps").mkdir(parents=True)
    root.chmod(0o750)

    # No playground/captures: a host that never ran a Playground capture.
    permissions.drop_legacy_evidence_acls(str(root), USER)

    assert f"group:{permissions.SHARED_GROUP}:r-x" in _acl(root / "dumps")


def test_legacy_evidence_refuses_multiply_linked_evidence(tmp_path):
    _require_acl_tools()
    if not _shared_group_exists():
        pytest.skip(f"the {permissions.SHARED_GROUP} group is not present on this host")
    root = _evidence_tree(tmp_path)
    os.link(root / "dumps/case1/mem.raw", root / "dumps/case1/copy.raw")

    with pytest.raises(SystemExit) as caught:
        permissions.drop_legacy_evidence_acls(str(root), USER)

    assert "refusing multiply-linked evidence during ACL migration" in str(caught.value)


# --------------------------------------------------------------------------
# subprocess entry point
# --------------------------------------------------------------------------


def test_the_module_runs_as_the_subprocess_the_installer_invokes(tmp_path):
    """The installer calls this by path, not by import, so exercise that path."""
    root = _tree(tmp_path)

    result = subprocess.run(
        [sys.executable, str(MODULE), "tree", str(root), USER, _own_group(), "0750", "0640", USER],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert _mode(root / "top.txt") == 0o640


def test_an_unknown_action_is_refused(tmp_path):
    result = subprocess.run([sys.executable, str(MODULE), "demolish", str(tmp_path)], capture_output=True, text=True)

    assert result.returncode != 0
    assert "unknown permissions.py action" in result.stderr


def test_no_action_is_refused():
    result = subprocess.run([sys.executable, str(MODULE)], capture_output=True, text=True)

    assert result.returncode != 0
    assert "usage: permissions.py" in result.stderr


# --------------------------------------------------------------------------
# ACL effectiveness: one reading, shared by all three verifying phases
# --------------------------------------------------------------------------
#
# A POSIX ACL entry only grants what the mask still allows. `getfacl` says so by
# appending "\t#effective:<perms>" to any entry the mask narrows. A phase that
# reads the entry and ignores the annotation reports success for access the
# kernel will refuse -- and these phases run as root over an operator's
# evidence, so "the ACL is in place" has to mean the access actually exists.

# Each fake below is the reading for ONE call, so every test drives a root with
# no entries in it: the phase then makes exactly one `set_acl`/`apply_acl`/
# `reconcile_acl` call, at a permission set the fake is written to match. A
# fixture with files in it would make the phase ask for "r--" as well, and the
# refusal could come from the spelling not matching rather than from the mask.

NEUTERED_USER_ACL = "user::rwx\nuser:{user}:r-x\t#effective:---\ngroup::---\nmask::---\nother::---\n"
DEFAULT_ONLY_ACL = "user::rwx\ngroup::---\nmask::r-x\nother::---\ndefault:user:{user}:r-x\n"
NEUTERED_GROUP_ACL = "user::rwx\ngroup::---\ngroup:{group}:--x\t#effective:---\nmask::---\nother::---\n"
EFFECTIVE_USER_ACL = "user::rwx\nuser:{user}:rwx\t#effective:r-x\ngroup::---\nmask::r-x\nother::---\n"


def _empty_root(base: Path, name: str) -> Path:
    root = base / name
    root.mkdir()
    root.chmod(0o755)
    return root


def test_ssh_keys_refuses_an_entry_the_mask_has_neutered(tmp_path, monkeypatch):
    _require_acl_tools()
    root = _empty_root(tmp_path, "ssh_keys")
    monkeypatch.setattr(permissions, "acl_text", lambda fd: NEUTERED_USER_ACL.format(user=USER))

    with pytest.raises(SystemExit) as caught:
        permissions.secure_ssh_keys(str(root), USER, USER, _own_group())

    assert "runner-only SSH key ACL is missing or ineffective" in str(caught.value)


def test_ssh_keys_refuses_when_only_the_default_entry_names_the_runner(tmp_path, monkeypatch):
    """A default entry grants nothing on the directory itself.

    `default:user:operator:r-x` contains the substring `user:operator:r-x`, so a reading
    that searches the whole ACL text accepts an inherited-only grant as though
    the runner could traverse the directory now.
    """
    _require_acl_tools()
    root = _empty_root(tmp_path, "ssh_keys")
    monkeypatch.setattr(permissions, "acl_text", lambda fd: DEFAULT_ONLY_ACL.format(user=USER))

    with pytest.raises(SystemExit) as caught:
        permissions.secure_ssh_keys(str(root), USER, USER, _own_group())

    assert "runner-only SSH key ACL is missing or ineffective" in str(caught.value)


def test_libvirt_images_refuses_an_entry_the_mask_has_neutered(tmp_path, monkeypatch):
    _require_acl_tools()
    root = _empty_root(tmp_path, "images")
    monkeypatch.setattr(permissions, "acl_text", lambda fd: NEUTERED_USER_ACL.format(user=USER))

    with pytest.raises(SystemExit) as caught:
        permissions.secure_libvirt_images(str(root), USER, [USER])

    assert "libvirt image ACL is missing or ineffective" in str(caught.value)


def test_legacy_evidence_refuses_an_entry_the_mask_has_neutered(tmp_path, monkeypatch):
    _require_acl_tools()
    if not _shared_group_exists():
        pytest.skip(f"the {permissions.SHARED_GROUP} group is not present on this host")
    # The root is the first object reconciled, and it asks for "--x".
    root = _empty_root(tmp_path, "storage")
    monkeypatch.setattr(permissions, "acl_text", lambda fd: NEUTERED_GROUP_ACL.format(group=permissions.SHARED_GROUP))

    with pytest.raises(SystemExit) as caught:
        permissions.drop_legacy_evidence_acls(str(root), USER)

    assert "new evidence-read ACL is missing or ineffective" in str(caught.value)


def test_acl_grants_reads_the_effective_permissions_not_the_entry():
    acl = EFFECTIVE_USER_ACL.format(user=USER)

    # The entry says rwx, the mask cuts it to r-x. r-x is what the kernel gives.
    assert permissions.acl_grants(acl, f"user:{USER}:", "r-x")
    assert not permissions.acl_grants(acl, f"user:{USER}:", "rwx")


def test_acl_grants_reads_an_unannotated_entry_at_face_value():
    acl = "user::rwx\nuser:someone:r-x\ngroup::---\nmask::r-x\nother::---\n"

    assert permissions.acl_grants(acl, "user:someone:", "r-x")
    assert not permissions.acl_grants(acl, "user:someone:", "rwx")


def test_acl_grants_ignores_a_default_entry_with_the_same_name():
    acl = "user::rwx\ngroup::---\nmask::r-x\nother::---\ndefault:user:someone:r-x\n"

    # The default entry is what a child will inherit, not what this object grants.
    assert not permissions.acl_grants(acl, "user:someone:", "r-x")


def test_acl_grants_is_false_when_the_entry_is_absent():
    acl = "user::rwx\ngroup::---\nmask::r-x\nother::---\n"

    assert not permissions.acl_grants(acl, "user:someone:", "r-x")


def test_acl_grants_does_not_confuse_a_group_entry_with_the_base_group():
    acl = f"user::rwx\ngroup::---\ngroup:{permissions.SHARED_GROUP}:r--\nmask::r--\nother::---\n"

    assert permissions.acl_grants(acl, f"group:{permissions.SHARED_GROUP}:", "r--")
    assert permissions.acl_grants(acl, "group::", "---")


@pytest.mark.parametrize("spelling", ["r-x", "rx"])
def test_acl_grants_canonicalises_both_sides(spelling):
    acl = f"user:someone:{spelling}\n"

    assert permissions.acl_grants(acl, "user:someone:", "r-x")


# --------------------------------------------------------------------------
# Equivalence with the three readings this one replaced
# --------------------------------------------------------------------------
#
# The three phases were separate heredocs, each verifying its ACL its own way.
# `acl_grants` replaced all three. These cases hold the historical readings as
# reference implementations and pin exactly where the new one agrees with them
# (every ACL the phases actually write) and where it deliberately does not
# (an entry the mask has neutered, and a default-only grant).


def _historical_ssh_keys_reading(acl: str, user: str, wanted: str) -> bool:
    """ops/deploy/install.sh, secure_ssh_key_permissions: two substring tests."""
    return "group::---" in acl and f"user:{user}:{wanted}" in acl


def _historical_libvirt_reading(acl: str, user: str, wanted: str) -> bool:
    """ops/deploy/install.sh, secure_libvirt_image_access: canonicalised, effective-aware."""
    prefix = f"user:{user}:"
    for line in acl.splitlines():
        if not line.startswith(prefix):
            continue
        entry, _, annotation = line.partition("\t#effective:")
        actual = annotation.strip() if annotation else entry[len(prefix) :]
        if _canonical_reference(actual) == _canonical_reference(wanted):
            return True
    return False


def _historical_legacy_reading(acl: str, group: str, wanted: str) -> bool:
    """ops/deploy/install.sh, remove_legacy_evidence_acls: raw trailing field."""
    for line in acl.splitlines():
        if not line.startswith(f"group:{group}:"):
            continue
        entry, marker, effective = line.partition("\t#effective:")
        actual = effective if marker else entry.rsplit(":", 1)[-1]
        if actual == wanted:
            return True
    return False


def _canonical_reference(value: str) -> str:
    return "".join(permission if permission in value else "-" for permission in "rwx")


# Exactly what each phase writes, as getfacl reports it back.
SSH_KEY_DIRECTORY_ACL = (
    "user::rwx\nuser:{user}:r-x\ngroup::---\nmask::r-x\nother::---\n"
    "default:user::rwx\ndefault:user:{user}:r-x\ndefault:group::---\ndefault:mask::r-x\ndefault:other::---\n"
)
SSH_KEY_FILE_ACL = "user::rw-\nuser:{user}:r--\ngroup::---\nmask::r--\nother::---\n"
IMAGE_DIRECTORY_ACL = "user::rwx\nuser:{user}:r-x\ngroup::r-x\nmask::r-x\nother::r-x\n"
IMAGE_FILE_ACL = "user::rw-\nuser:{user}:r--\ngroup::r--\nmask::r--\nother::r--\n"
EVIDENCE_DIRECTORY_ACL = "user::rwx\ngroup::---\ngroup:{group}:r-x\nmask::r-x\nother::---\n"
EVIDENCE_FILE_ACL = "user::rw-\ngroup::---\ngroup:{group}:r--\nmask::r--\nother::---\n"


@pytest.mark.parametrize("template,wanted", [(SSH_KEY_DIRECTORY_ACL, "r-x"), (SSH_KEY_FILE_ACL, "r--")])
def test_unified_reading_agrees_with_the_ssh_keys_reading_on_what_it_writes(template, wanted):
    acl = template.format(user=USER)

    assert _historical_ssh_keys_reading(acl, USER, wanted) is True
    assert permissions.acl_grants(acl, f"user:{USER}:", wanted) is True
    assert permissions.acl_grants(acl, "group::", "---") is True


@pytest.mark.parametrize("template,wanted", [(IMAGE_DIRECTORY_ACL, "r-x"), (IMAGE_FILE_ACL, "r--")])
def test_unified_reading_agrees_with_the_libvirt_reading_on_what_it_writes(template, wanted):
    acl = template.format(user=USER)

    assert _historical_libvirt_reading(acl, USER, wanted) is True
    assert permissions.acl_grants(acl, f"user:{USER}:", wanted) is True


@pytest.mark.parametrize("template,wanted", [(EVIDENCE_DIRECTORY_ACL, "r-x"), (EVIDENCE_FILE_ACL, "r--")])
def test_unified_reading_agrees_with_the_legacy_reading_on_what_it_writes(template, wanted):
    group = permissions.SHARED_GROUP
    acl = template.format(group=group)

    assert _historical_legacy_reading(acl, group, wanted) is True
    assert permissions.acl_grants(acl, f"group:{group}:", wanted) is True


def test_unified_reading_is_stricter_than_the_ssh_keys_reading_it_replaced():
    neutered = NEUTERED_USER_ACL.format(user=USER)
    default_only = DEFAULT_ONLY_ACL.format(user=USER)

    # The two cases the substring reading got wrong, and the whole reason for
    # replacing it: both are accepted by the old reading.
    assert _historical_ssh_keys_reading(neutered, USER, "r-x") is True
    assert _historical_ssh_keys_reading(default_only, USER, "r-x") is True

    assert permissions.acl_grants(neutered, f"user:{USER}:", "r-x") is False
    assert permissions.acl_grants(default_only, f"user:{USER}:", "r-x") is False
