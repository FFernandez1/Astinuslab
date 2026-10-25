"""Bounded self-relative descriptor metadata. Does not evaluate effective access."""

from __future__ import annotations

import hashlib
import struct


def describe(raw: bytes) -> dict:
    def require(value: bool) -> None:
        if not value:
            raise ValueError("descriptor_structure")

    require(type(raw) is bytes and 20 <= len(raw) <= 8192)
    revision, reserved, control, owner, group, sacl, dacl = struct.unpack_from("<BBHIIII", raw)
    require(revision == 1 and (reserved == 0 or bool(control & 0x4000)) and bool(control & 0x8000))
    require(not sacl or bool(control & 0x10))
    require(not dacl or bool(control & 0x04))
    regions: list[tuple[int, int, str]] = [(0, 20, "header")]
    basic_only = True

    def sid(offset: int, limit: int) -> tuple[str, int]:
        require(offset + 8 <= limit)
        rev, count = struct.unpack_from("<BB", raw, offset)
        require(rev == 1 and count <= 15)
        end = offset + 8 + count * 4
        require(end <= limit)
        authority = int.from_bytes(raw[offset + 2 : offset + 8], "big")
        subs = struct.unpack_from("<" + "I" * count, raw, offset + 8)
        return "S-1-" + str(authority) + "".join("-" + str(n) for n in subs), end

    def region(offset: int, end: int, kind: str) -> None:
        require(offset >= 20 and offset % 4 == 0 and end <= len(raw))
        for start, stop, other in regions:
            if offset < stop and start < end:
                # Windows may share the exact owner/group SID storage.
                require(kind == other == "sid" and (offset, end) == (start, stop))
        regions.append((offset, end, kind))

    def identity(offset: int) -> str | None:
        if not offset:
            return None
        value, end = sid(offset, len(raw))
        region(offset, end, "sid")
        return value

    def acl(offset: int) -> dict | None:
        nonlocal basic_only
        if not offset:
            return None
        require(offset + 8 <= len(raw))
        rev, pad, size, count, pad2 = struct.unpack_from("<BBHHH", raw, offset)
        require(rev in (2, 4) and pad == pad2 == 0 and size >= 8 and size % 4 == 0 and count <= 256)
        limit = offset + size
        region(offset, limit, "acl")
        cursor = offset + 8
        entries = []
        for _ in range(count):
            require(cursor + 4 <= limit)
            kind, flags, length = struct.unpack_from("<BBH", raw, cursor)
            require(length >= 4 and length % 4 == 0 and cursor + length <= limit)
            entry = {"type": kind, "flags": flags, "size": length}
            if kind in (0, 1, 2, 3):
                require(length >= 16)
                entry["mask"] = struct.unpack_from("<I", raw, cursor + 4)[0]
                entry["sid"], end = sid(cursor + 8, cursor + length)
                require(end == cursor + length)
            else:
                basic_only = False
                entry["sha256"] = hashlib.sha256(raw[cursor : cursor + length]).hexdigest()
            entries.append(entry)
            cursor += length
        require(not any(raw[cursor:limit]))
        return {"revision": rev, "size": size, "entries": entries}

    result = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "control": control,
        "owner": identity(owner),
        "group": identity(group),
        "sacl": acl(sacl),
        "dacl": acl(dacl),
    }
    covered = bytearray(len(raw))
    for start, end, _ in regions:
        covered[start:end] = b"\x01" * (end - start)
    require(not any(byte for byte, used in zip(raw, covered, strict=True) if not used))
    result["basic_aces_only"] = basic_only
    return result
