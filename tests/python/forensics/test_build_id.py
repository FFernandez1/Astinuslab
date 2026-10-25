from __future__ import annotations


def test_build_id_from_guest_prefers_sysfs_notes(monkeypatch):
    """Resolution order: /sys/kernel/notes -> debug vmlinux -> extract-vmlinux."""
    from pathlib import Path

    import astinuslab.forensics.symbols.build_id as mod

    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd

        name = b"GNU\0"
        desc = bytes.fromhex("deadbeef")
        blob = (4).to_bytes(4, "little") + (len(desc)).to_bytes(4, "little") + (3).to_bytes(4, "little") + name + desc

        class R:
            returncode = 0
            stdout = blob.hex()
            stderr = ""

        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    out = mod.build_id_from_guest("10.0.0.5", Path("/tmp/key"), "ubuntu", "6.8.0-test")

    assert out == "deadbeef"
    assert "/sys/kernel/notes" in " ".join(captured["cmd"])
