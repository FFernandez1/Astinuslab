"""Apply the ownership, modes and ACLs an installation's trees require.

Six installer phases hand a directory to the kernel and say who may reach it.
They share the same hazards -- a symlink planted mid-walk, an entry whose owner
this installation never created, a tree that crosses a filesystem boundary, a
`setfacl` that reports success while the mask makes the entry ineffective -- and
they used to share nothing else, because each one was a separate
``python3 - <<'PY'`` heredoc in ``ops/deploy/install.sh``. A heredoc body is a
shell string, so Ruff, Mypy and coverage never read any of them.

What is shared here is the primitives: the open flags, the allowed-owner
resolution, the one way to run `setfacl` and `getfacl` against an open
descriptor rather than a path, and :func:`acl_grants` -- the one reading of
whether an ACL entry actually grants what it claims.

That last one used to be three readings. Each phase was its own heredoc and
checked its freshly written ACL its own way: ``libvirt-images`` canonicalised
and honoured `#effective:`, ``drop-legacy-evidence-acl`` honoured it but
compared the raw field, and ``ssh-keys`` searched the whole ACL text for two
substrings. The third accepted two things it should not have -- an entry the
mask had reduced to nothing (the annotation it never read), and a
``default:user:runner:r-x`` line, whose text contains the entry it was looking
for but which grants nothing on the directory itself. All three now call
:func:`acl_grants`, which reads the effective permissions, anchors the entry
prefix at the start of a line, and canonicalises both sides. This is stricter
than what ``ssh-keys`` had and identical on every ACL the three phases write;
``tests/python/tooling/test_permissions.py`` keeps the historical readings as
reference implementations and pins both halves of that claim.

What is deliberately *not* shared is the walks. They differ in ways that are
not incidental:

* budgets differ -- the SSH key tree allows depth 16 and 4096 entries, the
  others depth 64 and 200,000;
* custody is enforced by ``tree``, ``libvirt-images`` and ``directory-root``,
  and deliberately not by the other three;
* an entry that cannot be opened aborts ``tree``, is skipped by
  ``drop-legacy-evidence-acl``, and raises through ``ssh-keys``;
* each subject words its refusals for its own tree, and those strings reach an
  operator's terminal.

Invoked as a subprocess by the shell function that owns each phase, which
passes the arguments it always passed after the subcommand:

    permissions.py tree ROOT OWNER GROUP DIR_MODE FILE_MODE [ALLOWED_OWNER...]
    permissions.py ssh-keys ROOT API_USER RUNNER_USER GROUP
    permissions.py libvirt-images ROOT QEMU_USER [ALLOWED_OWNER...]
    permissions.py directory-root ROOT OWNER GROUP MODE [ALLOWED_OWNER...]
    permissions.py managed-file INSTALL_DIR RELATIVE OWNER GROUP MODE
    permissions.py drop-legacy-evidence-acl STORAGE_ROOT LEGACY_USER
"""

from __future__ import annotations

import grp
import os
import pwd
import stat
import subprocess
import sys

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)

# The group every service identity shares. `drop-legacy-evidence-acl` grants to
# it by name rather than by argument, as the heredoc did.
SHARED_GROUP = "astinuslab"


class EntryBudget:
    """One walk's depth and entry allowance, with the subject's own wording.

    The heredocs each carried a module-level ``seen`` counter shared by the
    whole recursion. One budget instance per invocation is the same thing with
    a name, and keeps two subcommands in one process from sharing a count.
    """

    def __init__(self, *, depth: int, entries: int, depth_message: str, entry_message: str) -> None:
        self.depth = depth
        self.entries = entries
        self.depth_message = depth_message
        self.entry_message = entry_message
        self.seen = 0

    def enter(self, depth: int) -> None:
        if depth > self.depth:
            raise SystemExit(self.depth_message)

    def count(self) -> None:
        self.seen += 1
        if self.seen > self.entries:
            raise SystemExit(self.entry_message)


def resolve_allowed_owners(names: list[str]) -> set[int]:
    """Root plus every named identity that exists, ignoring those that do not.

    An installation names identities it may not have created yet, and an empty
    string is how the shell passes an absent optional owner. Both resolve to
    "not an allowed owner" rather than to an error.
    """
    allowed = {0}
    for name in names:
        try:
            allowed.add(pwd.getpwnam(name).pw_uid)
        except KeyError:
            pass
    return allowed


def _fd_path(descriptor: int) -> str:
    """Address an open descriptor, so no path is re-resolved between checks."""
    return f"/proc/self/fd/{descriptor}"


def setfacl(descriptor: int, *arguments: str) -> None:
    subprocess.run(["setfacl", *arguments, "--", _fd_path(descriptor)], check=True, pass_fds=(descriptor,))


def acl_text(descriptor: int) -> str:
    return subprocess.run(
        ["getfacl", "-cp", "--", _fd_path(descriptor)],
        check=True,
        capture_output=True,
        text=True,
        pass_fds=(descriptor,),
    ).stdout


def _canonical(value: str) -> str:
    """Normalise an ACL permission field to a full `rwx`-shaped triple."""
    return "".join(permission if permission in value else "-" for permission in "rwx")


def acl_grants(acl: str, prefix: str, permissions: str) -> bool:
    """Whether the entry named by ``prefix`` is *effective* at ``permissions``.

    A POSIX ACL entry only grants what the mask still allows, and `getfacl`
    says so by appending ``\t#effective:<perms>`` to any entry the mask
    narrows. Reading the entry and ignoring that annotation reports success for
    access the kernel will refuse.

    ``prefix`` is matched at the start of a line, so ``user:runner:`` does not
    match ``default:user:runner:``. A default entry is what a child will
    inherit; it grants nothing on the object being checked, and treating the
    two as the same accepts an inherited-only grant as a present one.
    """
    for line in acl.splitlines():
        if not line.startswith(prefix):
            continue
        entry, marker, effective = line.partition("\t#effective:")
        actual = effective.strip() if marker else entry[len(prefix) :]
        if _canonical(actual) == _canonical(permissions):
            return True
    return False


def _scandir_names(directory_fd: int) -> list[str]:
    with os.scandir(directory_fd) as entries:
        return [entry.name for entry in entries]


# --------------------------------------------------------------------------
# tree: ownership and modes for a tree this installation owns outright.
# --------------------------------------------------------------------------


def secure_tree(
    root: str,
    owner_name: str,
    group_name: str,
    directory_mode_text: str,
    file_mode_text: str,
    extra_names: list[str],
) -> None:
    owner = pwd.getpwnam(owner_name).pw_uid
    group = grp.getgrnam(group_name).gr_gid
    allowed_owners = resolve_allowed_owners(extra_names)
    directory_mode = int(directory_mode_text, 8)
    file_mode = int(file_mode_text, 8)
    budget = EntryBudget(
        depth=64,
        entries=200_000,
        depth_message="mutable tree exceeds depth limit",
        entry_message="mutable tree exceeds entry limit",
    )

    def reconcile(directory_fd: int, *, device: int, depth: int) -> None:
        budget.enter(depth)
        for name in _scandir_names(directory_fd):
            budget.count()
            try:
                fd = os.open(name, FILE_FLAGS, dir_fd=directory_fd)
            except OSError as exc:
                raise SystemExit(f"refusing unsupported mutable-tree entry: {name}") from exc
            try:
                info = os.fstat(fd)
                if info.st_uid not in allowed_owners:
                    raise SystemExit(f"refusing mutable-tree entry with foreign custody: {name}")
                if info.st_dev != device:
                    raise SystemExit(f"refusing cross-device mutable-tree entry: {name}")
                if stat.S_ISDIR(info.st_mode):
                    os.close(fd)
                    fd = os.open(name, DIRECTORY_FLAGS, dir_fd=directory_fd)
                    os.fchown(fd, owner, group)
                    os.fchmod(fd, directory_mode)
                    reconcile(fd, device=device, depth=depth + 1)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    os.fchown(fd, owner, group)
                    os.fchmod(fd, file_mode)
                else:
                    raise SystemExit(f"refusing non-regular or multiply-linked mutable-tree entry: {name}")
            finally:
                os.close(fd)

    root_fd = os.open(root, DIRECTORY_FLAGS)
    try:
        root_info = os.fstat(root_fd)
        if root_info.st_uid not in allowed_owners:
            raise SystemExit("refusing mutable-tree root with foreign custody")
        os.fchown(root_fd, owner, group)
        os.fchmod(root_fd, directory_mode)
        reconcile(root_fd, device=root_info.st_dev, depth=0)
    finally:
        os.close(root_fd)


# --------------------------------------------------------------------------
# ssh-keys: the API identity owns the keys; the runner may only read them.
# --------------------------------------------------------------------------


def secure_ssh_keys(root: str, api_name: str, runner_name: str, group_name: str) -> None:
    api_uid = pwd.getpwnam(api_name).pw_uid
    # The ACL names the runner rather than its uid, but resolve it anyway: a
    # missing runner account must fail here, as it did in the heredoc, and not
    # later as a setfacl error against a name nothing can resolve.
    pwd.getpwnam(runner_name)
    group_gid = grp.getgrnam(group_name).gr_gid
    budget = EntryBudget(
        depth=16,
        entries=4096,
        depth_message="SSH key tree exceeds depth limit",
        entry_message="SSH key tree exceeds entry limit",
    )

    def set_acl(fd: int, *, directory: bool) -> None:
        os.fchown(fd, api_uid, group_gid)
        os.fchmod(fd, 0o2700 if directory else 0o600)
        # Clear first: an inherited or pre-existing entry would otherwise
        # survive alongside the one entry this tree is allowed to carry.
        setfacl(fd, "-b")
        permissions = "r-x" if directory else "r--"
        entries = [f"u:{runner_name}:{permissions}", f"m::{permissions}"]
        if directory:
            entries.extend([f"d:u:{runner_name}:r-x", "d:g::---", "d:m::r-x", "d:o::---"])
        setfacl(fd, "-m", ",".join(entries))
        acl = acl_text(fd)
        if not acl_grants(acl, "group::", "---") or not acl_grants(acl, f"user:{runner_name}:", permissions):
            raise SystemExit("runner-only SSH key ACL is missing or ineffective")

    def reconcile(directory_fd: int, *, device: int, depth: int) -> None:
        budget.enter(depth)
        set_acl(directory_fd, directory=True)
        for name in _scandir_names(directory_fd):
            budget.count()
            fd = os.open(name, FILE_FLAGS, dir_fd=directory_fd)
            try:
                info = os.fstat(fd)
                if info.st_dev != device:
                    raise SystemExit("SSH key tree crosses a filesystem boundary")
                if stat.S_ISDIR(info.st_mode):
                    os.close(fd)
                    fd = os.open(name, DIRECTORY_FLAGS, dir_fd=directory_fd)
                    reconcile(fd, device=device, depth=depth + 1)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    set_acl(fd, directory=False)
                else:
                    raise SystemExit("SSH key tree contains a non-regular or multiply-linked entry")
            finally:
                os.close(fd)

    root_fd = os.open(root, DIRECTORY_FLAGS)
    try:
        reconcile(root_fd, device=os.fstat(root_fd).st_dev, depth=0)
    finally:
        os.close(root_fd)


# --------------------------------------------------------------------------
# libvirt-images: qemu must read the definitions without owning them.
# --------------------------------------------------------------------------


def _group_permissions(mode: int) -> str:
    bits = (mode & 0o070) >> 3
    return "".join(name for name, bit in (("r", 4), ("w", 2), ("x", 1)) if bits & bit)


def secure_libvirt_images(root: str, qemu_name: str, owner_names: list[str]) -> None:
    allowed_owners = resolve_allowed_owners(owner_names)
    # Unlike the optional identities above, qemu's own account must exist: it is
    # the identity the whole phase grants to.
    allowed_owners.add(pwd.getpwnam(qemu_name).pw_uid)
    budget = EntryBudget(
        depth=64,
        entries=200_000,
        depth_message="image tree exceeds depth limit",
        entry_message="image tree exceeds entry limit",
    )

    def apply_acl(fd: int, *, directory: bool) -> None:
        permissions = "r-x" if directory else "r--"
        # The mask has to cover what the group already had as well as what this
        # entry adds, or granting qemu access would silently remove someone's.
        effective = set(_group_permissions(os.fstat(fd).st_mode)) | set(permissions.replace("-", ""))
        mask = "".join(permission for permission in "rwx" if permission in effective) or "---"
        setfacl(fd, "-m", f"u:{qemu_name}:{permissions},m::{mask}")
        if not acl_grants(acl_text(fd), f"user:{qemu_name}:", permissions):
            raise SystemExit("libvirt image ACL is missing or ineffective")

    def reconcile(directory_fd: int, *, device: int, depth: int) -> None:
        budget.enter(depth)
        info = os.fstat(directory_fd)
        if info.st_uid not in allowed_owners or info.st_dev != device:
            raise SystemExit("image directory has foreign custody or crosses a filesystem boundary")
        apply_acl(directory_fd, directory=True)
        for name in _scandir_names(directory_fd):
            budget.count()
            fd = os.open(name, FILE_FLAGS, dir_fd=directory_fd)
            try:
                child = os.fstat(fd)
                if child.st_uid not in allowed_owners or child.st_dev != device:
                    raise SystemExit("image entry has foreign custody or crosses a filesystem boundary")
                if stat.S_ISDIR(child.st_mode):
                    os.close(fd)
                    fd = os.open(name, DIRECTORY_FLAGS, dir_fd=directory_fd)
                    reconcile(fd, device=device, depth=depth + 1)
                elif stat.S_ISREG(child.st_mode) and child.st_nlink == 1:
                    apply_acl(fd, directory=False)
                else:
                    raise SystemExit("image tree contains a non-regular or multiply-linked entry")
            finally:
                os.close(fd)

    root_fd = os.open(root, DIRECTORY_FLAGS)
    try:
        reconcile(root_fd, device=os.fstat(root_fd).st_dev, depth=0)
    finally:
        os.close(root_fd)


# --------------------------------------------------------------------------
# directory-root and managed-file: single objects, no walk.
# --------------------------------------------------------------------------


def secure_directory_root(root: str, owner_name: str, group_name: str, mode_text: str, names: list[str]) -> None:
    allowed_owners = resolve_allowed_owners(names)
    descriptor = os.open(root, DIRECTORY_FLAGS)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in allowed_owners:
            raise SystemExit("refusing managed storage root with foreign custody")
        os.fchown(descriptor, pwd.getpwnam(owner_name).pw_uid, grp.getgrnam(group_name).gr_gid)
        os.fchmod(descriptor, int(mode_text, 8))
    finally:
        os.close(descriptor)


def secure_managed_file(root: str, relative: str, owner_name: str, group_name: str, mode_text: str) -> None:
    segments = relative.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise SystemExit("unsafe managed-file path")
    owner = pwd.getpwnam(owner_name).pw_uid
    group = grp.getgrnam(group_name).gr_gid
    descriptor = os.open(root, DIRECTORY_FLAGS)
    try:
        for segment in segments[:-1]:
            child = os.open(segment, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(segments[-1], FILE_FLAGS, dir_fd=descriptor)
        try:
            info = os.fstat(file_descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SystemExit("managed file is non-regular or multiply linked")
            os.fchown(file_descriptor, owner, group)
            os.fchmod(file_descriptor, int(mode_text, 8))
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


# --------------------------------------------------------------------------
# drop-legacy-evidence-acl: replace a per-user grant with the shared group.
# --------------------------------------------------------------------------


def drop_legacy_evidence_acls(root: str, legacy_user: str) -> None:
    budget = EntryBudget(
        depth=64,
        entries=200_000,
        depth_message="legacy evidence tree exceeds depth limit",
        entry_message="legacy evidence tree exceeds entry limit",
    )

    def reconcile_acl(fd: int, group_permissions: str) -> None:
        if any(line.startswith(f"user:{legacy_user}:") for line in acl_text(fd).splitlines()):
            setfacl(fd, "-x", f"u:{legacy_user}")
        setfacl(fd, "-m", f"g:{SHARED_GROUP}:{group_permissions}")
        if not acl_grants(acl_text(fd), f"group:{SHARED_GROUP}:", group_permissions):
            raise SystemExit("new evidence-read ACL is missing or ineffective")

    def walk(directory_fd: int, *, device: int, depth: int) -> None:
        budget.enter(depth)
        reconcile_acl(directory_fd, "r-x")
        for name in _scandir_names(directory_fd):
            budget.count()
            try:
                fd = os.open(name, FILE_FLAGS, dir_fd=directory_fd)
            except OSError:
                # A migration sweep is not the place to fail over one entry it
                # cannot open; the phases that must refuse do so above.
                continue
            try:
                info = os.fstat(fd)
                if info.st_dev != device:
                    raise SystemExit("legacy evidence tree crosses a filesystem boundary")
                if stat.S_ISDIR(info.st_mode):
                    os.close(fd)
                    fd = os.open(name, DIRECTORY_FLAGS, dir_fd=directory_fd)
                    walk(fd, device=device, depth=depth + 1)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    reconcile_acl(fd, "r--")
                elif stat.S_ISREG(info.st_mode):
                    raise SystemExit("refusing multiply-linked evidence during ACL migration")
            finally:
                os.close(fd)

    root_fd = os.open(root, DIRECTORY_FLAGS)
    try:
        root_info = os.fstat(root_fd)
        reconcile_acl(root_fd, "--x")
        # Only the evidence trees are traversed. The root and any intermediate
        # directory get traverse-only, so the grant does not widen the rest.
        for segments in (("dumps",), ("playground", "captures")):
            parent_fd = os.dup(root_fd)
            try:
                for index, segment in enumerate(segments):
                    next_fd = os.open(segment, DIRECTORY_FLAGS, dir_fd=parent_fd)
                    reconcile_acl(next_fd, "r-x" if index == len(segments) - 1 else "--x")
                    os.close(parent_fd)
                    parent_fd = next_fd
                walk(parent_fd, device=root_info.st_dev, depth=0)
            except FileNotFoundError:
                pass
            finally:
                os.close(parent_fd)
    finally:
        os.close(root_fd)


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit("usage: permissions.py ACTION ...")
    action, arguments = argv[0], argv[1:]
    if action == "tree":
        secure_tree(arguments[0], arguments[1], arguments[2], arguments[3], arguments[4], arguments[5:])
    elif action == "ssh-keys":
        secure_ssh_keys(arguments[0], arguments[1], arguments[2], arguments[3])
    elif action == "libvirt-images":
        secure_libvirt_images(arguments[0], arguments[1], arguments[2:])
    elif action == "directory-root":
        secure_directory_root(arguments[0], arguments[1], arguments[2], arguments[3], arguments[4:])
    elif action == "managed-file":
        secure_managed_file(arguments[0], arguments[1], arguments[2], arguments[3], arguments[4])
    elif action == "drop-legacy-evidence-acl":
        drop_legacy_evidence_acls(arguments[0], arguments[1])
    else:
        raise SystemExit(f"unknown permissions.py action: {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
