"""Public descriptor/transport checks; these do not prove native Windows access."""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
import platform
import shlex
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

MODULES = Path(__file__).resolve().parents[3] / "tools/dev/windows_ntfs_acl"


def perl(expression: str, *args: str) -> str:
    result = subprocess.run(
        ["perl", "-I", str(MODULES), "-MRestrictedAcl", "-MJSON::PP", "-e", expression, *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def descriptor() -> bytes:
    return bytes.fromhex(perl("print unpack('H*', RestrictedAcl::descriptor())"))


def test_exact_protected_descriptor_layout():
    raw = descriptor()
    assert len(raw) == 100
    assert struct.unpack_from("<BBHIIII", raw) == (1, 0, 0x9004, 72, 84, 0, 20)
    assert struct.unpack_from("<BBHHH", raw, 20) == (2, 0, 52, 2, 0)
    system = bytes.fromhex("010100000000000512000000")
    admins = bytes.fromhex("01020000000000052000000020020000")
    assert raw[72:84] == system
    assert raw[84:] == admins
    assert raw[28:48] == struct.pack("<BBHI", 0, 0, 20, 0x1F01FF) + system
    assert raw[48:72] == struct.pack("<BBHI", 0, 0, 24, 0x1F01FF) + admins


def test_setter_transports_only_ascii_hex_to_appliance_absolute_path():
    command = perl("print RestrictedAcl::setter_command($ARGV[0])", "/Windows/Panther/unattend.xml")
    assert command.isascii() and "\x00" not in command
    assert "--value=0x" + descriptor().hex() in command
    assert "--no-dereference" in command
    assert "--name=system.ntfs_acl" in command
    assert "-- /sysroot/Windows/Panther/unattend.xml" in command
    assert "/usr/bin/setfattr" in command


@pytest.mark.parametrize(
    "path",
    [
        "",
        "probe.txt",
        "/",
        "//probe.txt",
        "/../probe.txt",
        "/./x",
        "/a/../x",
        "/x;id",
        "/x$(id)",
        "/x\n",
        "/x'",
        "/x ",
        "/x\\y",
        "/é",
        "/" + "a" * 260,
    ],
)
def test_rejects_unsafe_path_before_constructing_shell(path):
    assert (
        perl("eval { RestrictedAcl::setter_command($ARGV[0]) }; print $@ ? 'rejected' : 'accepted'", path) == "rejected"
    )


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("ASTINUS_ACL_EXIT=0", 0),
        ("ASTINUS_ACL_EXIT=1", 1),
        ("ASTINUS_ACL_EXIT=255", 255),
        ("", None),
        ("ASTINUS_ACL_EXIT=0\n", None),
        ("ASTINUS_ACL_EXIT=0 ", None),
        ("ASTINUS_ACL_EXIT=256\n", None),
        ("ASTINUS_ACL_EXIT=00\n", None),
        ("ASTINUS_ACL_EXIT=0\nsecret", None),
        ("ASTINUS_ACL_EXIT=-1\n", None),
    ],
)
def test_bounded_setter_reply_is_exit_status_not_syscall_errno(reply, expected):
    result = json.loads(
        perl("my $n=eval { RestrictedAcl::setter_exit($ARGV[0]) }; print encode_json({value=>$n})", reply)
    )
    assert result["value"] == expected


def test_installed_setfattr_decodes_all_binary_bytes_on_disposable_host_file(tmp_path):
    setter = shutil.which("setfattr")
    if setter is None:
        pytest.skip("installed attr tool required for executable binary encoding check")
    path = tmp_path / "encoding-only"
    path.touch(mode=0o600)
    raw = descriptor()
    # Exercise the emitted shell command too. Substitute ONLY the disposable
    # host destination and user xattr namespace; never set a host NTFS ACL.
    command = perl("print RestrictedAcl::setter_command('/probe.txt')")
    command = command.replace("system.ntfs_acl", "user.astinus_binary_probe").replace(
        "/sysroot/probe.txt", shlex.quote(str(path))
    )
    result = subprocess.run(["/bin/sh", "-c", command], check=True, capture_output=True, timeout=10)
    assert result.stdout == b"ASTINUS_ACL_EXIT=0" and not result.stderr
    assert os.getxattr(path, "user.astinus_binary_probe", follow_symlinks=False) == raw

    link = tmp_path / "link"
    link.symlink_to(path)
    os.removexattr(path, "user.astinus_binary_probe")
    command = command.replace(shlex.quote(str(path)), shlex.quote(str(link)))
    result = subprocess.run(["/bin/sh", "-c", command], check=True, capture_output=True, timeout=10)
    assert result.stdout == b"ASTINUS_ACL_EXIT=1" and not result.stderr
    assert "user.astinus_binary_probe" not in os.listxattr(path)


def test_capability_checks_use_actual_tools_and_libguestfs_reply_framing():
    path = Path("/usr/bin/setfattr")
    if not path.is_file():
        pytest.skip("installed attr tool required")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    checks = json.loads(perl("print encode_json([RestrictedAcl::capability_checks($ARGV[0])])", digest))
    assert [row[0] for row in checks] == [
        "capability_root",
        "capability_tools",
        "capability_version",
        "capability_digest",
    ]
    for phase, command, expected in checks:
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            env={"root": "/sysroot", "LC_ALL": "C"},
            capture_output=True,
            check=True,
            timeout=10,
        )
        # daemon/debug.c uses FOLD_STDOUT_ON_STDERR. daemon/command.c removes
        # trailing LF bytes from that buffer. Other bytes remain exact.
        assert not result.stderr
        assert result.stdout.rstrip(b"\n").decode("ascii") == expected, phase
        assert not expected.endswith("\n")
    wrong_root = subprocess.run(
        ["/bin/sh", "-c", checks[0][1]], env={"root": "/wrong"}, capture_output=True, timeout=10
    )
    assert wrong_root.returncode != 0 and not wrong_root.stdout


def test_synthetic_collector_guards_and_failure_phases():
    harness = Path(__file__).parents[1] / "_support/windows_ntfs_acl_probe.pl"
    result = subprocess.run(["perl", "-I", str(MODULES), str(harness)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not ok" not in result.stdout


def test_ascii_setter_command_survives_installed_xdr_string_encoding():
    library = ctypes.util.find_library("tirpc")
    if library is None or platform.machine() != "x86_64":
        pytest.skip("native libtirpc x86_64 transport characterization")

    class XDR(ctypes.Structure):
        # Installed libtirpc rpc/xdr.h ABI. ENCODE into memory only: no RPC,
        # guestfs handle, decoder, disk or unsafe binary setter is involved.
        _fields_ = [
            ("op", ctypes.c_int),
            ("ops", ctypes.POINTER(ctypes.c_void_p)),
            ("public", ctypes.c_void_p),
            ("private", ctypes.c_void_p),
            ("base", ctypes.c_void_p),
            ("handy", ctypes.c_uint),
        ]

    assert ctypes.sizeof(XDR) == 48
    lib = ctypes.CDLL(library)
    pointer = ctypes.POINTER(XDR)
    lib.xdrmem_create.argtypes = [pointer, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int]
    lib.xdrmem_create.restype = None
    lib.xdr_string.argtypes = [pointer, ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint]
    lib.xdr_string.restype = ctypes.c_int
    command = perl("print RestrictedAcl::setter_command('/probe.txt')").encode("ascii")
    value = ctypes.create_string_buffer(command)
    value_pointer = ctypes.c_void_p(ctypes.addressof(value))
    output = ctypes.create_string_buffer(4096)
    stream = XDR()
    lib.xdrmem_create(ctypes.byref(stream), output, len(output), 0)
    destroy = ctypes.CFUNCTYPE(None, pointer)(stream.ops[7])
    try:
        assert lib.xdr_string(ctypes.byref(stream), ctypes.byref(value_pointer), 4096) == 1
        assert output.raw[:4] == struct.pack(">I", len(command))
        assert output.raw[4 : 4 + len(command)] == command
    finally:
        destroy(ctypes.byref(stream))
