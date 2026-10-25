"""Read and remove the privileged host state the deploy scripts act on.

Every phase here answers the same question before anything irreversible
happens: what is actually at this path, and does its ownership allow acting on
it? They answer it the same way -- resolve component by component with
``O_NOFOLLOW``, prove the descriptor's identity and ownership, then act through
that descriptor rather than resolving the path a second time. All four were
`python3 - <<'PY'` heredocs, and a heredoc body is a shell string, so Ruff,
Mypy and coverage never read any of it. Here they do.

``split-identity-marker`` is shared. The marker decides whether an installation
runs under one service user or three, so ``install.sh``,
``configure-privileged-roots.sh`` and ``uninstall.sh`` must all read it exactly
alike; they held three byte-identical copies, kept in step by a test that
compared them. One implementation replaces both the copies and the comparison.

What reverses the installer's *recorded* host access is not here. The traversal
ACL and MAC policy manifests are written by ``ops/deploy/host_access.py`` and
reversed by its ``revoke-all`` subcommand, so the format has one reader and one
writer instead of two of each.

Invoked as a subprocess by the shell function that owns each phase:

    privileged_state.py install-dir-authority INSTALL_DIR
    privileged_state.py split-identity-marker IDENTITIES_FILE
    privileged_state.py remove-privileged-helpers HELPER_DIR NAME [NAME ...]
    privileged_state.py clean-legacy-vol3-bin INSTALL_DIR
"""

from __future__ import annotations

import errno
import os
import stat
import sys

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

SPLIT_IDENTITY_MARKER = (
    b"mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n"
)

LEGACY_VOL3_NAMES = ("README.md", "dwarf2json-linux-amd64", "dwarf2json-linux-amd64.sha256")


def install_dir_authority(path: str) -> None:
    """Refuse an install root whose existing ancestors are not root-controlled.

    This runs before any service is touched. `rm -rf $INSTALL_DIR` later is only
    safe if nobody but root could have arranged what that path resolves to, so
    every component that exists must be a real directory, root:root, and not
    group- or other-writable.
    """
    parts = [part for part in path.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise SystemExit("INSTALL_DIR must not contain dot path components")
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        for part in parts:
            try:
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                break
            except OSError as exc:
                raise SystemExit(f"INSTALL_DIR ancestor must be a real non-symlink directory: {part}: {exc}") from exc
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022:
                raise SystemExit(
                    "INSTALL_DIR and every existing ancestor must be root:root and non-writable by group/other"
                )
    finally:
        os.close(descriptor)


def split_identity_marker(path: str) -> int:
    """Exit status saying whether the split-identity marker is trustworthy.

    Whether the install used one service user or three decides which users the
    uninstall offers to delete, so the marker is only believed when it is a
    root-owned regular file, not group/other-writable, holding exactly the bytes
    the installer writes. Anything else reads as "not a split install".
    """
    try:
        descriptor = os.open(path, READ_FLAGS)
    except OSError:
        return 1
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            return 1
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            if handle.read() != SPLIT_IDENTITY_MARKER:
                return 1
    finally:
        os.close(descriptor)
    return 0


def remove_privileged_helpers(path: str, managed_names: list[str]) -> None:
    """Remove the helper set this project installed, and only if nothing else is there.

    The directory holds executables reachable through a sudoers rule. Removing an
    unknown file from it would destroy something another package owns, so an
    unexpected entry stops the removal and is reported instead.
    """
    try:
        directory_fd = os.open(path, DIRECTORY_FLAGS)
    except OSError as exc:
        raise SystemExit(f"privileged helper root is not a safe non-symlink directory: {exc}") from exc
    try:
        identity = os.fstat(directory_fd)
        if not stat.S_ISDIR(identity.st_mode):
            raise SystemExit("privileged helper root is not a directory")
        for name in managed_names:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        remaining = os.listdir(directory_fd)
        if remaining:
            raise SystemExit(
                "privileged helper directory still contains unknown files: " + ", ".join(sorted(remaining))
            )
        current = os.lstat(path)
        if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (
            identity.st_dev,
            identity.st_ino,
        ):
            raise SystemExit("privileged helper root changed during cleanup")
    finally:
        os.close(directory_fd)
    os.rmdir(path)


def clean_legacy_vol3_bin(root: str) -> None:
    """Remove the three third-party files releases before the tool boundary installed.

    `vol3/bin/` is operator-owned now, so this never recurses and never removes
    anything but those three names. The directory goes only if removing them
    leaves it empty. Reports what it did on stdout, one fact per line, because
    the caller turns each into an operator-facing message.
    """
    try:
        root_fd = os.open(root, DIRECTORY_FLAGS)
    except FileNotFoundError:
        print("skipped")
        return
    vol3_fd = -1
    bin_fd = -1
    try:
        root_info = os.fstat(root_fd)
        try:
            vol3_fd = os.open("vol3", DIRECTORY_FLAGS, dir_fd=root_fd)
            bin_fd = os.open("bin", DIRECTORY_FLAGS, dir_fd=vol3_fd)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                print("skipped")
                return
            raise
        bin_info = os.fstat(bin_fd)
        if bin_info.st_dev != root_info.st_dev:
            print("skipped-cross-device")
            return
        removed = False
        for name in LEGACY_VOL3_NAMES:
            try:
                descriptor = os.open(name, READ_FLAGS | os.O_NONBLOCK, dir_fd=bin_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENXIO, errno.ENODEV, errno.EACCES}:
                    continue
                raise
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != bin_info.st_dev:
                    continue
                current = os.stat(name, dir_fd=bin_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise RuntimeError(f"legacy vol3 entry changed during cleanup: {name}")
                os.unlink(name, dir_fd=bin_fd)
                print(f"removed:{name}")
                removed = True
            finally:
                os.close(descriptor)
        remaining = sorted(entry.name for entry in os.scandir(bin_fd))
        if remaining:
            print("remaining:" + " ".join(remaining))
        elif removed:
            current = os.stat("bin", dir_fd=vol3_fd, follow_symlinks=False)
            if stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (
                bin_info.st_dev,
                bin_info.st_ino,
            ):
                os.rmdir("bin", dir_fd=vol3_fd)
                print("removed-dir")
    finally:
        if bin_fd >= 0:
            os.close(bin_fd)
        if vol3_fd >= 0:
            os.close(vol3_fd)
        os.close(root_fd)


def main(argv: list[str]) -> None:
    if len(argv) == 2 and argv[0] == "install-dir-authority":
        install_dir_authority(argv[1])
    elif len(argv) == 2 and argv[0] == "split-identity-marker":
        raise SystemExit(split_identity_marker(argv[1]))
    elif len(argv) >= 3 and argv[0] == "remove-privileged-helpers":
        remove_privileged_helpers(argv[1], argv[2:])
    elif len(argv) == 2 and argv[0] == "clean-legacy-vol3-bin":
        clean_legacy_vol3_bin(argv[1])
    else:
        raise SystemExit(
            "usage: privileged_state.py install-dir-authority INSTALL_DIR"
            " | split-identity-marker IDENTITIES_FILE"
            " | remove-privileged-helpers HELPER_DIR NAME [NAME ...]"
            " | clean-legacy-vol3-bin INSTALL_DIR"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
