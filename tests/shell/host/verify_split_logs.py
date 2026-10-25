#!/usr/bin/env python3
"""Read-only row-8 acceptance: current role logs and the installed API reader."""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import re
import select
import stat
import subprocess
import sys
import time
from pathlib import Path

FILES = {"api": "api.log", "runner": "runner.log", "analysis": "analysis-worker.log"}
FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def directory(path: Path) -> int:
    require(path.is_absolute() and ".." not in path.parts, "noncanonical log path")
    descriptor = os.open("/", FLAGS | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, FLAGS | os.O_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def acl(descriptor: int, deadline: float) -> set[str]:
    require(time.monotonic() < deadline, "log proof deadline exceeded")
    result = subprocess.run(
        ["getfacl", "-cnp", "--", f"/proc/self/fd/{descriptor}"],
        pass_fds=(descriptor,),
        capture_output=True,
        text=True,
        check=True,
        timeout=max(0.1, min(5, deadline - time.monotonic())),
    )
    return {line.split("#", 1)[0].strip() for line in result.stdout.splitlines() if line and not line.startswith("#")}


def capture(command: list[str], deadline: float, limit: int) -> bytes:
    """Bound bytes and elapsed time without printing journal contents."""
    expires = min(deadline, time.monotonic() + 5)
    require(time.monotonic() < expires, "log proof deadline exceeded")
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    output = bytearray()
    try:
        require(process.stdout is not None, "journal capture unavailable")
        if process.stdout is None:
            raise ValueError("journal capture unavailable")
        descriptor = process.stdout.fileno()
        while True:
            remaining = expires - time.monotonic()
            if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
                raise TimeoutError("current invocation journal deadline exceeded")
            block = os.read(descriptor, min(8192, limit + 1 - len(output)))
            if not block:
                break
            output.extend(block)
            require(len(output) <= limit, "current invocation journal exceeds byte limit")
        require(process.wait(timeout=max(0.1, expires - time.monotonic())) == 0, "current invocation query failed")
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=1)
        if process.stdout is not None:
            process.stdout.close()


def current_invocation_logs(role: str, deadline: float) -> None:
    command = ["systemctl", "show", "astinuslab-" + role + ".service", "-p", "InvocationID", "--value"]
    invocation = capture(command, deadline, 128).strip()
    require(
        re.fullmatch(rb"[0-9a-f]{32}", invocation) is not None and invocation != b"0" * 32, "invalid current invocation"
    )
    journal = capture(
        [
            "journalctl",
            "--quiet",
            "--no-pager",
            "--all",
            "-o",
            "cat",
            "--lines=201",
            "_SYSTEMD_INVOCATION_ID=" + invocation.decode("ascii"),
        ],
        deadline,
        128 * 1024,
    )
    # A 201-line tail could omit an earlier warning. Refuse that ambiguity;
    # an empty journal also cannot establish current-invocation logging health.
    require(
        bool(journal.strip()) and len(journal.splitlines()) < 201, "current invocation journal completeness is unproven"
    )
    require(b"file logging disabled" not in journal.lower(), "current invocation reports file logging disabled")
    require(capture(command, deadline, 128).strip() == invocation, "current invocation changed during log proof")


def verify(install: Path, storage: Path, *, wait_seconds: int = 20) -> None:
    require(os.geteuid() == 0 and 0 <= wait_seconds <= 20, "root and bounded wait required")
    deadline = time.monotonic() + 25
    wait_deadline = time.monotonic() + wait_seconds
    current = (install / "current").resolve(strict=True)
    require(current.parent == install / "releases", "installed release is outside managed releases")
    roles = {role: pwd.getpwnam("astinuslab-" + role).pw_uid for role in FILES}
    require(min(roles.values()) > 0 and len(set(roles.values())) == 3, "three unprivileged roles required")
    group = grp.getgrnam("astinuslab").gr_gid
    api = roles["api"]
    anchor = directory(storage / "logs")
    try:
        info = os.fstat(anchor)
        require((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (0, 0, 0o1770), "unsafe log anchor")
        marker = os.open(".astinuslab-service-logs.json", FLAGS, dir_fd=anchor)
        try:
            info = os.fstat(marker)
            require(
                stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 4096, "unsafe log marker type"
            )
            require(
                (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (0, 0, 0o644), "unsafe log marker custody"
            )
            payload = json.loads(os.read(marker, 4097))
            require(
                payload == {"schema": "astinuslab-service-logs/v1", "roles": roles}
                and all(type(uid) is int for uid in payload["roles"].values()),
                "invalid role schema",
            )
        finally:
            os.close(marker)
        expected = {
            "user::rwx",
            "group::---",
            "mask::rwx",
            "other::---",
            f"user:{api}:r-x",
            f"user:{roles['runner']}:rwx",
            f"user:{roles['analysis']}:--x",
        }
        qemu = set()
        for name in ("libvirt-qemu", "qemu"):
            try:
                qemu.add(f"user:{pwd.getpwnam(name).pw_uid}:--x")
            except KeyError:
                pass
        actual = acl(anchor, deadline)
        require(expected <= actual and not actual - expected - qemu, "unexpected log anchor ACL")
        for role, filename in FILES.items():
            child = os.open(role, FLAGS | os.O_DIRECTORY, dir_fd=anchor)
            try:
                info = os.fstat(child)
                require(
                    (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (roles[role], 0, 0o770),
                    "unsafe role directory",
                )
                expected = {
                    "user::rwx",
                    f"user:{api}:r-x",
                    "group::---",
                    "mask::rwx",
                    "other::---",
                    "default:user::rwx",
                    f"default:user:{api}:rwx",
                    "default:group::---",
                    "default:mask::rwx",
                    "default:other::---",
                }
                require(acl(child, deadline) == expected, "unexpected role directory ACL")
                while True:
                    descriptor = -1
                    try:
                        descriptor = os.open(filename, FLAGS, dir_fd=child)
                        info = os.fstat(descriptor)
                        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe role log type")
                        require(
                            info.st_uid == roles[role]
                            and info.st_gid in (0, group)
                            and stat.S_IMODE(info.st_mode) == 0o660,
                            "unsafe role log custody",
                        )
                        actual = acl(descriptor, deadline)
                        expected = {"user::rw-", "group::---", "mask::rw-", "other::---"}
                        # A newly inherited ACL can retain x behind the rw mask.
                        require(
                            actual - {f"user:{api}:rw-", f"user:{api}:rwx"} == expected and len(actual) == 5,
                            "unexpected role log ACL",
                        )
                        if info.st_size:
                            break
                    except FileNotFoundError:
                        pass
                    finally:
                        if descriptor >= 0:
                            os.close(descriptor)
                    if time.monotonic() >= wait_deadline:
                        raise TimeoutError("role log did not become nonempty: " + role)
                    time.sleep(min(0.25, wait_deadline - time.monotonic()))
            finally:
                os.close(child)
            code = "from pathlib import Path; import sys; from astinuslab.core.log_layout import open_log\nwith open_log(Path(sys.argv[1]), sys.argv[2]) as stream:\n if not stream.read(1): raise SystemExit(1)"
            require(time.monotonic() < deadline, "log proof deadline exceeded")
            result = subprocess.run(
                [
                    "runuser",
                    "-u",
                    "astinuslab-api",
                    "--",
                    str(current / ".venv/bin/python"),
                    "-I",
                    "-B",
                    "-c",
                    code,
                    str(storage),
                    filename,
                ],
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(0.1, min(5, deadline - time.monotonic())),
            )
            require(result.returncode == 0, "installed API log reader refused: " + role)
            current_invocation_logs(role, deadline)
    finally:
        os.close(anchor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", type=Path, required=True)
    parser.add_argument("--storage", type=Path, required=True)
    args = parser.parse_args()
    verify(args.install, args.storage)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError, KeyError) as error:
        print(f"split role-log acceptance failed ({type(error).__name__})", file=sys.stderr)
        raise SystemExit(1) from None
