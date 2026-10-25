from __future__ import annotations

import binascii
import struct
import subprocess
from pathlib import Path


def _build_id_from_notes_blob(data: bytes) -> str | None:
    offset = 0
    while offset + 12 <= len(data):
        namesz, descsz, note_type = struct.unpack_from("<III", data, offset)
        offset += 12
        name = data[offset : offset + namesz]
        offset += (namesz + 3) & ~3
        desc = data[offset : offset + descsz]
        offset += (descsz + 3) & ~3
        if note_type == 3 and name.rstrip(b"\x00") == b"GNU" and desc:
            return desc.hex()
    return None


def _normalize_build_id_output(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    last_line = value.splitlines()[-1].strip()
    try:
        parsed = _build_id_from_notes_blob(binascii.unhexlify(last_line))
    except (binascii.Error, ValueError):
        parsed = None
    return (parsed or last_line).lower()


def build_id_from_guest(ip_address: str, ssh_key: Path, guest_user: str, kernel_release: str) -> str | None:
    remote = rf"""
set -e
if [ -r /sys/kernel/notes ]; then
    python3 -c 'import sys, struct
data = sys.stdin.buffer.read()
o = 0
while o + 12 <= len(data):
    ns, ds, t = struct.unpack_from("<III", data, o); o += 12
    name = data[o:o+ns]; o += (ns + 3) & ~3
    desc = data[o:o+ds]; o += (ds + 3) & ~3
    if t == 3 and name.rstrip(b"\x00") == b"GNU":
        sys.stdout.write(desc.hex()); sys.exit(0)
sys.exit(1)' < /sys/kernel/notes && exit 0
fi
for p in /usr/lib/debug/boot/vmlinux-{kernel_release} /usr/lib/debug/lib/modules/{kernel_release}/vmlinux; do
    if [ -f "$p" ]; then
        (eu-readelf -n "$p" 2>/dev/null || readelf -n "$p" 2>/dev/null) | awk '/Build ID/{{print $NF; exit}}' && exit 0
    fi
done
if command -v extract-vmlinux >/dev/null 2>&1 && [ -f /boot/vmlinuz-{kernel_release} ]; then
    tmp=$(mktemp)
    if extract-vmlinux /boot/vmlinuz-{kernel_release} > "$tmp" 2>/dev/null; then
        (eu-readelf -n "$tmp" 2>/dev/null || readelf -n "$tmp" 2>/dev/null) | awk '/Build ID/{{print $NF; exit}}'
        rm -f "$tmp"
        exit 0
    fi
    rm -f "$tmp"
fi
exit 1
"""
    cmd = [
        "ssh",
        "-i",
        str(ssh_key),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "BatchMode=yes",
        f"{guest_user}@{ip_address}",
        remote,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _normalize_build_id_output(result.stdout)
