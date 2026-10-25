"""Inspect, or create and activate, the installation's SSH key pair.

The product drives guest VMs over SSH, so this key is the credential the whole
job path depends on. Every entry is addressed by descriptor and published by
linking an already-validated inode, never by reopening a pathname: the
directory it writes into is under `storage/`, which service identities can
reach while the installer is working in it.

The three outcomes are printed on stdout for `handle_ssh_key` to read, one
line, pipe-separated:

    missing|            no key directory, or no active key, and none generated
    existing|NAME       NAME is already active and passed every custody check
    generated|NAME      NAME was created, published and activated by this run

This was a 227-line `python3 - <<'PY'` heredoc in ops/deploy/install.sh, the
largest one left after the ACL and host-access extractions, and the only
checks that ever reached it were `compile()` and pyflakes.

Invoked as a subprocess by `inspect_or_generate_ssh_key`, with exactly the
arguments it always passed:

    ssh_key.py INSTALL_ROOT OWNER GROUP GENERATE DIRECTORY_MODE
"""

from __future__ import annotations

import ctypes
import grp
import os
import pwd
import re
import secrets
import stat
import subprocess
import sys

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _open_child_directory(parent_fd: int, name: str, *, create: bool = False) -> int:
    try:
        return os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        return os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)


def _safe_regular(directory_fd: int, name: str, *, private: bool = False) -> tuple[int, os.stat_result]:
    descriptor = os.open(name, FILE_FLAGS, dir_fd=directory_fd)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise RuntimeError(f"unsafe SSH key entry: {name}")
    if private and info.st_mode & 0o027:
        os.close(descriptor)
        raise RuntimeError(f"SSH private key has unsafe permissions: {name}")
    return descriptor, info


def _read_active(directory_fd: int, *, owner: int, group: int) -> str | None:
    try:
        descriptor, info = _safe_regular(directory_fd, ".active")
    except FileNotFoundError:
        return None
    try:
        if info.st_mode & 0o022 or info.st_size > 128 or (info.st_uid, info.st_gid) != (owner, group):
            raise RuntimeError("unsafe SSH active-key marker")
        payload = os.read(descriptor, 129)
    finally:
        os.close(descriptor)
    try:
        name = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError("invalid SSH active-key marker") from exc
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) is None or name in {".", ".."}:
        raise RuntimeError("invalid SSH active-key name")
    private_fd, private_info = _safe_regular(directory_fd, name, private=True)
    os.close(private_fd)
    public_fd, public_info = _safe_regular(directory_fd, name + ".pub")
    os.close(public_fd)
    if (private_info.st_uid, private_info.st_gid) != (owner, group) or (
        public_info.st_uid,
        public_info.st_gid,
    ) != (owner, group):
        raise RuntimeError("active SSH key has unexpected ownership")
    return name


def _same_inode(directory_fd: int, name: str, expected: os.stat_result) -> bool:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return not stat.S_ISLNK(current.st_mode) and (current.st_dev, current.st_ino) == (
        expected.st_dev,
        expected.st_ino,
    )


def _link_open_file(source_fd: int, directory_fd: int, name: str) -> None:
    """Publish the already-validated inode without reopening its pathname."""
    libc = ctypes.CDLL(None, use_errno=True)
    linkat = libc.linkat
    linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    linkat.restype = ctypes.c_int
    at_empty_path = 0x1000
    if linkat(source_fd, b"", directory_fd, os.fsencode(name), at_empty_path) == 0:
        return
    error = ctypes.get_errno()
    if error not in {1, 2, 13, 22, 95}:
        raise OSError(error, os.strerror(error), name)
    # Unprivileged unit tests do not hold CAP_DAC_READ_SEARCH, which Linux
    # requires for AT_EMPTY_PATH. /proc/self/fd still names the bound inode;
    # following that descriptor symlink never reopens the attacker-controlled
    # temporary pathname.
    os.link(
        f"/proc/self/fd/{source_fd}",
        name,
        dst_dir_fd=directory_fd,
        follow_symlinks=True,
    )


def inspect_or_generate(
    install_root: str,
    owner_name: str,
    group_name: str,
    generate_text: str,
    directory_mode_text: str,
) -> None:
    """Report the active key, or generate one when *generate_text* is "true"."""
    generate = generate_text == "true"
    owner = pwd.getpwnam(owner_name).pw_uid
    group = grp.getgrnam(group_name).gr_gid
    directory_mode = int(directory_mode_text, 8)
    install_fd = os.open(install_root, DIRECTORY_FLAGS)
    storage_fd = -1
    keys_fd = -1
    try:
        try:
            storage_fd = _open_child_directory(install_fd, "storage")
        except FileNotFoundError as exc:
            raise RuntimeError("storage directory is missing before SSH key setup") from exc
        try:
            keys_fd = _open_child_directory(storage_fd, "ssh_keys", create=generate)
        except FileNotFoundError:
            print("missing|")
            raise SystemExit(0) from None
        keys_identity = os.fstat(keys_fd)
        active = _read_active(keys_fd, owner=owner, group=group)
        if active is not None:
            if not _same_inode(storage_fd, "ssh_keys", keys_identity):
                raise RuntimeError("SSH key directory changed during inspection")
            print(f"existing|{active}")
            raise SystemExit(0)
        if not generate:
            print("missing|")
            raise SystemExit(0)

        # Quiesced installation code temporarily owns the directory while it
        # publishes. This prevents a retained shared-group principal from swapping
        # temp names or final entries around the descriptor checks.
        os.fchown(keys_fd, os.geteuid(), group)
        os.fchmod(keys_fd, 0o700)
        keys_identity = os.fstat(keys_fd)

        key_name = "astinuslab-default"
        for final_name in (key_name, key_name + ".pub"):
            try:
                os.stat(final_name, dir_fd=keys_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise RuntimeError(f"refusing to overwrite inactive SSH key entry: {final_name}")

        nonce = secrets.token_hex(12)
        temporary_private = f".{key_name}.tmp.{nonce}"
        temporary_public = temporary_private + ".pub"
        marker_temporary = f".active.tmp.{nonce}"
        published: list[tuple[str, os.stat_result]] = []
        try:
            key_path = f"/proc/self/fd/{keys_fd}/{temporary_private}"
            subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", "", "-C", f"astinuslab/{key_name}"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                pass_fds=(keys_fd,),
            )
            private_fd, private_info = _safe_regular(keys_fd, temporary_private, private=True)
            public_fd, public_info = _safe_regular(keys_fd, temporary_public)
            try:
                os.fchmod(private_fd, 0o600)
                os.fchown(private_fd, owner, group)
                os.fsync(private_fd)
                os.fchmod(public_fd, 0o640)
                os.fchown(public_fd, owner, group)
                os.fsync(public_fd)
                private_info = os.fstat(private_fd)
                public_info = os.fstat(public_fd)
                for source_fd, temporary, final_name, expected in (
                    (private_fd, temporary_private, key_name, private_info),
                    (public_fd, temporary_public, key_name + ".pub", public_info),
                ):
                    _link_open_file(source_fd, keys_fd, final_name)
                    published.append((final_name, expected))
                    if not _same_inode(keys_fd, final_name, expected):
                        raise RuntimeError(f"SSH key changed during publication: {final_name}")
                    os.unlink(temporary, dir_fd=keys_fd)
            finally:
                os.close(private_fd)
                os.close(public_fd)

            marker_fd = os.open(
                marker_temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                0o640,
                dir_fd=keys_fd,
            )
            try:
                os.write(marker_fd, (key_name + "\n").encode())
                os.fchown(marker_fd, owner, group)
                os.fsync(marker_fd)
                marker_info = os.fstat(marker_fd)
                _link_open_file(marker_fd, keys_fd, ".active")
                os.unlink(marker_temporary, dir_fd=keys_fd)
            finally:
                os.close(marker_fd)
            if not _same_inode(keys_fd, ".active", marker_info):
                raise RuntimeError("SSH active-key marker changed during publication")
            os.fsync(keys_fd)
            if not _same_inode(storage_fd, "ssh_keys", keys_identity):
                raise RuntimeError("SSH key directory changed during publication")
        except BaseException:
            for temporary in (temporary_private, temporary_public, marker_temporary):
                try:
                    os.unlink(temporary, dir_fd=keys_fd)
                except FileNotFoundError:
                    pass
            if "marker_info" in locals() and _same_inode(keys_fd, ".active", marker_info):
                os.unlink(".active", dir_fd=keys_fd)
            for final_name, expected in reversed(published):
                if _same_inode(keys_fd, final_name, expected):
                    os.unlink(final_name, dir_fd=keys_fd)
            raise
        print(f"generated|{key_name}")
    finally:
        if keys_fd >= 0:
            # The surrounding permission reconciler supplies split runner ACLs;
            # this restores exact owner/mode even on a failed generation attempt.
            os.fchown(keys_fd, owner, group)
            os.fchmod(keys_fd, directory_mode)
            os.close(keys_fd)
        if storage_fd >= 0:
            os.close(storage_fd)
        os.close(install_fd)


def main(argv: list[str]) -> None:
    if len(argv) != 5:
        raise SystemExit("usage: ssh_key.py INSTALL_ROOT OWNER GROUP GENERATE DIRECTORY_MODE")
    inspect_or_generate(*argv)


if __name__ == "__main__":
    main(sys.argv[1:])
