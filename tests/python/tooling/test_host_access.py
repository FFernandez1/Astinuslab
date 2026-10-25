"""Contract for `ops/deploy/host_access.py`.

The installer walks the storage path to `/` adding `u:<qemu>:x` to every
component so qemu can reach a capture. That walk crosses directories the
product does not own, so what it granted has to be written down, and rollback
has to take back exactly that and nothing else.

The writer used to live in a heredoc in ops/deploy/install.sh and the reader in
a heredoc in ops/deploy/configure-privileged-roots.sh, and the two had to agree
about a tab-separated record byte for byte by inspection. They are one module
now, so the agreement is checkable: these cases pin the record format itself,
then the grant and rollback paths that depend on it.

The shell entry points keep their own coverage in test_install_script.py and
test_uninstall_acl_reversal.py.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ops.deploy import host_access

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "ops/deploy/host_access.py"
INSTALL = ROOT / "ops/deploy/install.sh"
PRIVILEGED_ROOTS = ROOT / "ops/deploy/configure-privileged-roots.sh"
GRANTEE = "nobody"
pytestmark = [pytest.mark.unit, pytest.mark.host]


def _require_acl_tools() -> None:
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("ACL tools are not available")
    if subprocess.run(["getent", "passwd", GRANTEE], capture_output=True).returncode != 0:
        pytest.skip(f"{GRANTEE} user is not available")


def _named_entry(path: Path, user: str = GRANTEE) -> str | None:
    text = subprocess.run(["getfacl", "-cp", "--", str(path)], capture_output=True, text=True, check=True).stdout
    for line in text.splitlines():
        if line.startswith(f"user:{user}:"):
            return line
    return None


def _protected_log(path: Path) -> Path:
    path.write_text("", encoding="utf-8")
    path.chmod(0o600)
    return path


# --------------------------------------------------------------------------
# The record format the two former heredocs had to agree about.
# --------------------------------------------------------------------------


def test_a_traversal_record_is_five_tab_separated_fields(tmp_path):
    """Pinned as literal bytes, not as a round trip through the module.

    A round trip alone would still pass if both halves changed together, and a
    manifest written by an installed release is read back by an uninstall that
    may be a different version.
    """
    line = host_access.format_traversal_record(GRANTEE, 2049, 1234, "/srv/storage")

    assert line == f"v2\t{GRANTEE}\t2049\t1234\t/srv/storage"


def test_a_traversal_record_reads_back_to_what_was_written():
    line = host_access.format_traversal_record(GRANTEE, 2049, 1234, "/srv/storage")

    user, identity, path = host_access.parse_traversal_record(line + "\n")

    assert (user, identity, path) == (GRANTEE, (2049, 1234), Path("/srv/storage"))


@pytest.mark.parametrize(
    "raw",
    [
        "v1\tnobody\t2049\t1234\t/srv/storage\n",
        "nobody\t2049\t1234\t/srv/storage\n",
        "v2\tnobody\t2049\t1234\n",
    ],
)
def test_a_record_this_installation_did_not_write_is_refused(raw):
    with pytest.raises(ValueError):
        host_access.parse_traversal_record(raw)


def test_both_scripts_call_the_one_module_that_owns_the_format():
    """The extraction's point: one module, not two heredocs that must agree."""
    installer = INSTALL.read_text(encoding="utf-8")
    privileged = PRIVILEGED_ROOTS.read_text(encoding="utf-8")

    assert 'python3 "$SCRIPT_DIR/host_access.py" grant' in installer
    assert 'python3 "$SCRIPT_DIR/host_access.py" record' in installer
    assert 'python3 "$SCRIPT_DIR/host_access.py" rollback' in privileged
    assert "v2\\t" not in installer and "v2\\t" not in privileged


# --------------------------------------------------------------------------
# The transaction log.
# --------------------------------------------------------------------------


def test_a_transaction_record_is_one_sorted_json_line(tmp_path):
    log = _protected_log(tmp_path / "host-access.transaction")

    host_access.record(str(log), "apparmor", "/etc/apparmor.d/libvirt", "  /srv/** rwk,", "", "")
    host_access.record(str(log), "selinux", "/srv", "/srv(/.*)?", "selinux_type", "svirt_image_t")

    lines = log.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"kind": "apparmor", "target": "/etc/apparmor.d/libvirt", "value": "  /srv/** rwk,"},
        {
            "kind": "selinux",
            "selinux_type": "svirt_image_t",
            "target": "/srv",
            "value": "/srv(/.*)?",
        },
    ]
    assert lines[0].index('"kind"') < lines[0].index('"target"') < lines[0].index('"value"')


def test_a_transaction_log_anyone_else_can_write_is_refused(tmp_path):
    """The log is rollback evidence: a writable one could forge a revocation."""
    log = _protected_log(tmp_path / "host-access.transaction")
    log.chmod(0o622)

    with pytest.raises(SystemExit, match="protected regular installer-owned file"):
        host_access.record(str(log), "apparmor", "/etc/apparmor.d/libvirt", "rule", "", "")

    assert log.read_text(encoding="utf-8") == ""


def test_a_transaction_log_that_is_a_symlink_is_never_followed(tmp_path):
    target = _protected_log(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(OSError):
        host_access.record(str(link), "apparmor", "/etc/apparmor.d/libvirt", "rule", "", "")

    assert target.read_text(encoding="utf-8") == ""


# --------------------------------------------------------------------------
# Granting traversal.
# --------------------------------------------------------------------------


def test_grant_adds_execute_to_every_component_and_records_each_one(tmp_path):
    _require_acl_tools()
    storage = tmp_path / "a/b/storage"
    storage.mkdir(parents=True)
    manifest = tmp_path / "manifest"
    log = _protected_log(tmp_path / "transaction")

    host_access.grant(str(storage), str(tmp_path), GRANTEE, str(manifest), str(log))

    for component in (tmp_path / "a", tmp_path / "a/b", storage):
        assert _named_entry(component) == f"user:{GRANTEE}:--x"
    recorded = manifest.read_text(encoding="utf-8").splitlines()
    assert [host_access.parse_traversal_record(line)[2] for line in recorded] == [
        tmp_path / "a",
        tmp_path / "a/b",
        storage,
    ]
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert [json.loads(line)["target"] for line in log.read_text(encoding="utf-8").splitlines()] == [
        str(tmp_path / "a"),
        str(tmp_path / "a/b"),
        str(storage),
    ]


def test_grant_leaves_an_entry_the_operator_already_had_out_of_the_manifest(tmp_path):
    """A component that already carried the entry was never ours to grant."""
    _require_acl_tools()
    storage = tmp_path / "a/storage"
    storage.mkdir(parents=True)
    preexisting = tmp_path / "a"
    subprocess.run(["setfacl", "-m", f"u:{GRANTEE}:rx", str(preexisting)], check=True)
    manifest = tmp_path / "manifest"

    host_access.grant(str(storage), str(tmp_path), GRANTEE, str(manifest), "")

    recorded = manifest.read_text(encoding="utf-8").splitlines()
    assert [host_access.parse_traversal_record(line)[2] for line in recorded] == [storage]
    assert _named_entry(preexisting) == f"user:{GRANTEE}:r-x", "an operator's own entry stays as it was"


def test_grant_refuses_an_existing_entry_without_effective_execute(tmp_path):
    """Presence is not proof of traversal: the mask can clamp the entry away."""
    _require_acl_tools()
    storage = tmp_path / "storage"
    storage.mkdir()
    subprocess.run(["setfacl", "-m", f"u:{GRANTEE}:r--,m::r--", str(storage)], check=True)
    manifest = tmp_path / "manifest"

    with pytest.raises(SystemExit, match="lacks effective execute"):
        host_access.grant(str(storage), str(tmp_path), GRANTEE, str(manifest), "")

    assert not manifest.exists(), "an operator-owned entry must not become ours"


def test_grant_is_idempotent(tmp_path):
    _require_acl_tools()
    storage = tmp_path / "a/storage"
    storage.mkdir(parents=True)
    manifest = tmp_path / "manifest"

    host_access.grant(str(storage), str(tmp_path), GRANTEE, str(manifest), "")
    first = manifest.read_text(encoding="utf-8")
    host_access.grant(str(storage), str(tmp_path), GRANTEE, str(manifest), "")

    assert manifest.read_text(encoding="utf-8") == first


def test_grant_refuses_a_stop_that_is_not_an_ancestor(tmp_path):
    _require_acl_tools()
    storage = tmp_path / "storage"
    storage.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()

    with pytest.raises(SystemExit, match="is not an ancestor of"):
        host_access.grant(str(storage), str(unrelated), GRANTEE, str(tmp_path / "manifest"), "")


@pytest.mark.parametrize("path", ["/srv/with\ttab", "/srv/with\nnewline"])
def test_grant_refuses_a_path_it_could_not_record_unambiguously(tmp_path, path):
    """The manifest is tab-separated and line-oriented; both are unrecordable."""
    with pytest.raises(SystemExit, match="control character"):
        host_access.grant(path, str(tmp_path), GRANTEE, str(tmp_path / "manifest"), "")


# --------------------------------------------------------------------------
# Taking it back.
# --------------------------------------------------------------------------


def test_revoke_removes_only_the_entry_the_record_describes(tmp_path):
    _require_acl_tools()
    storage = tmp_path / "storage"
    storage.mkdir()
    manifest = tmp_path / "manifest"
    host_access.grant(str(storage), str(tmp_path), GRANTEE, str(manifest), "")

    assert host_access.revoke_acl(manifest.read_text(encoding="utf-8")) is host_access.Reversal.REVERSED

    assert _named_entry(storage) is None


def test_revoke_reports_failure_when_the_path_changed_identity(tmp_path, capsys):
    _require_acl_tools()
    storage = tmp_path / "storage"
    storage.mkdir()
    manifest = tmp_path / "manifest"
    host_access.grant(str(storage), str(tmp_path), GRANTEE, str(manifest), "")
    line = manifest.read_text(encoding="utf-8")

    # Built before the original is removed so the kernel cannot hand the
    # replacement the same inode and make the identity check pass by accident.
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    subprocess.run(["setfacl", "-m", f"u:{GRANTEE}:x", str(replacement)], check=True)
    storage.rmdir()
    replacement.rename(storage)
    assert storage.stat().st_ino != host_access.parse_traversal_record(line)[1][1]

    assert host_access.revoke_acl(line) is host_access.Reversal.FAILED
    assert "identity changed" in capsys.readouterr().err
    assert _named_entry(storage) is not None, "an entry we cannot prove is ours stays put"


def test_revoke_accepts_a_path_that_is_already_gone(tmp_path):
    """An absent target has nothing left to reverse, which is not the same as reversed.

    Both outcomes let the line leave the manifest, which is all `rollback` asks --
    so the value stays truthy. `revoke_all` needs the difference, because it counts
    what it changed on the host and prints that for an operator.
    """
    line = host_access.format_traversal_record(GRANTEE, 2049, 1234, str(tmp_path / "absent")) + "\n"

    outcome = host_access.revoke_acl(line)

    assert outcome is host_access.Reversal.NOTHING_TO_REVERSE
    assert outcome, "a line with nothing to reverse still drains from the manifest"


def test_revoke_reports_nothing_to_reverse_for_a_component_carrying_no_entry(tmp_path):
    """The other way a record can have nothing to undo: the entry is already gone.

    The path is still there and still the inode that was recorded, so this is not
    a failure -- the ACL this installation added has simply been removed already,
    by an operator or an earlier uninstall.
    """
    _require_acl_tools()
    untouched = tmp_path / "untouched"
    untouched.mkdir()
    info = untouched.stat()
    line = host_access.format_traversal_record(GRANTEE, info.st_dev, info.st_ino, str(untouched)) + "\n"

    outcome = host_access.revoke_acl(line)

    assert outcome is host_access.Reversal.NOTHING_TO_REVERSE
    assert outcome


def test_a_reversal_outcome_is_falsy_only_when_it_failed():
    """The truthiness `rollback` was written against, stated once.

    `rollback` asks one question of every line -- may this leave the manifest? --
    and asks it as `if not revoke(raw)`. Making `NOTHING_TO_REVERSE` falsy would
    make rollback keep every already-clean line forever.
    """
    assert bool(host_access.Reversal.REVERSED) is True
    assert bool(host_access.Reversal.NOTHING_TO_REVERSE) is True
    assert bool(host_access.Reversal.FAILED) is False


def test_rollback_revokes_what_this_installation_added_and_keeps_the_baseline(tmp_path):
    _require_acl_tools()
    operator_owned = tmp_path / "operator"
    ours = tmp_path / "ours"
    operator_owned.mkdir()
    ours.mkdir()
    for path in (operator_owned, ours):
        subprocess.run(["setfacl", "-m", f"u:{GRANTEE}:x", str(path)], check=True)

    baseline_line = host_access.format_traversal_record(
        GRANTEE, operator_owned.stat().st_dev, operator_owned.stat().st_ino, str(operator_owned)
    )
    ours_line = host_access.format_traversal_record(GRANTEE, ours.stat().st_dev, ours.stat().st_ino, str(ours))
    acl_before = tmp_path / "traversal-acl.before"
    acl_before.write_text(baseline_line + "\n", encoding="utf-8")
    acl_manifest = tmp_path / "traversal-acl.manifest"
    acl_manifest.write_text(f"{baseline_line}\n{ours_line}\n", encoding="utf-8")
    acl_manifest.chmod(0o600)
    mac_before = tmp_path / "mac-policy.before"
    mac_before.write_text("", encoding="utf-8")

    status = host_access.rollback(
        str(acl_manifest),
        str(acl_before),
        "true",
        str(tmp_path / "mac-policy.manifest"),
        str(mac_before),
        "false",
        str(tmp_path),
        "",
    )

    assert status == 0
    assert _named_entry(ours) is None
    assert _named_entry(operator_owned) is not None
    assert acl_manifest.read_text(encoding="utf-8") == baseline_line + "\n"


def test_rollback_keeps_a_line_it_could_not_revoke_and_fails(tmp_path, capsys):
    """A manifest line survives a failed revoke so a retry can still find it."""
    _require_acl_tools()
    ours = tmp_path / "ours"
    ours.mkdir()
    subprocess.run(["setfacl", "-m", f"u:{GRANTEE}:rx", str(ours)], check=True)
    stale = host_access.format_traversal_record(GRANTEE, ours.stat().st_dev, ours.stat().st_ino, str(ours))
    acl_manifest = tmp_path / "traversal-acl.manifest"
    acl_manifest.write_text(stale + "\n", encoding="utf-8")
    acl_manifest.chmod(0o600)
    acl_before = tmp_path / "traversal-acl.before"
    acl_before.write_text("", encoding="utf-8")
    mac_before = tmp_path / "mac-policy.before"
    mac_before.write_text("", encoding="utf-8")

    status = host_access.rollback(
        str(acl_manifest),
        str(acl_before),
        "false",
        str(tmp_path / "mac-policy.manifest"),
        str(mac_before),
        "false",
        str(tmp_path),
        "",
    )

    assert status == 1
    assert "ACL changed since installation" in capsys.readouterr().err
    assert acl_manifest.read_text(encoding="utf-8") == stale + "\n"
    assert _named_entry(ours) == f"user:{GRANTEE}:r-x"


def test_rollback_replays_the_transaction_log_in_reverse(tmp_path):
    """Supplementary evidence for grants whose durable record went missing."""
    _require_acl_tools()
    first = tmp_path / "first"
    second = tmp_path / "first/second"
    second.mkdir(parents=True)
    log = _protected_log(tmp_path / "transaction")
    host_access.grant(str(second), str(tmp_path), GRANTEE, str(tmp_path / "manifest"), str(log))
    (tmp_path / "manifest").unlink()

    for path in (tmp_path / "traversal-acl.before", tmp_path / "mac-policy.before"):
        path.write_text("", encoding="utf-8")

    status = host_access.rollback(
        str(tmp_path / "traversal-acl.manifest"),
        str(tmp_path / "traversal-acl.before"),
        "false",
        str(tmp_path / "mac-policy.manifest"),
        str(tmp_path / "mac-policy.before"),
        "false",
        str(tmp_path),
        str(log),
    )

    assert status == 0
    assert _named_entry(first) is None
    assert _named_entry(second) is None


def test_rollback_refuses_a_transaction_log_anyone_else_can_write(tmp_path):
    log = _protected_log(tmp_path / "transaction")
    host_access.record(str(log), "apparmor", "/etc/apparmor.d/libvirt", "rule", "", "")
    log.chmod(0o622)
    for path in (tmp_path / "traversal-acl.before", tmp_path / "mac-policy.before"):
        path.write_text("", encoding="utf-8")

    with pytest.raises(SystemExit, match="protected regular installer-owned file"):
        host_access.rollback(
            str(tmp_path / "traversal-acl.manifest"),
            str(tmp_path / "traversal-acl.before"),
            "false",
            str(tmp_path / "mac-policy.manifest"),
            str(tmp_path / "mac-policy.before"),
            "false",
            str(tmp_path),
            str(log),
        )


def test_rollback_reports_a_transaction_record_it_does_not_understand(tmp_path, capsys):
    log = _protected_log(tmp_path / "transaction")
    host_access.record(str(log), "firewall", "/etc/nftables.conf", "rule", "", "")
    for path in (tmp_path / "traversal-acl.before", tmp_path / "mac-policy.before"):
        path.write_text("", encoding="utf-8")

    status = host_access.rollback(
        str(tmp_path / "traversal-acl.manifest"),
        str(tmp_path / "traversal-acl.before"),
        "false",
        str(tmp_path / "mac-policy.manifest"),
        str(tmp_path / "mac-policy.before"),
        "false",
        str(tmp_path),
        str(log),
    )

    assert status == 1
    assert "unsupported record" in capsys.readouterr().err


def test_rollback_removes_an_apparmor_line_it_added(tmp_path):
    """Exercise protected root custody with a fake ownership view and reload."""
    if shutil.which("fakeroot") is None:
        pytest.skip("fakeroot is not available")
    template = tmp_path / "libvirt.template"
    template.write_text("profile {\n  /existing rwk,\n  /srv/** rwk,\n}\n", encoding="utf-8")
    template.chmod(0o644)
    mac_manifest = tmp_path / "mac-policy.manifest"
    entry = json.dumps(
        {"kind": "apparmor", "target": str(template), "value": "  /srv/** rwk,", "version": 2},
        sort_keys=True,
    )
    mac_manifest.write_text(entry + "\n", encoding="utf-8")
    mac_manifest.chmod(0o600)
    for path in (tmp_path / "traversal-acl.before", tmp_path / "mac-policy.before"):
        path.write_text("", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    reload_log = tmp_path / "reload.calls"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$RELOAD_LOG"\n', encoding="utf-8")
    systemctl.chmod(0o755)

    result = subprocess.run(
        [
            "fakeroot",
            sys.executable,
            str(MODULE),
            "rollback",
            str(tmp_path / "traversal-acl.manifest"),
            str(tmp_path / "traversal-acl.before"),
            "false",
            str(mac_manifest),
            str(tmp_path / "mac-policy.before"),
            "true",
            str(tmp_path),
            "",
        ],
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "RELOAD_LOG": str(reload_log)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert template.read_text(encoding="utf-8") == "profile {\n  /existing rwk,\n}\n"
    assert reload_log.read_text(encoding="utf-8").splitlines() == ["restart apparmor", "restart libvirtd"]


# --------------------------------------------------------------------------
# Taking the whole installation back: `revoke-all`, which uninstall.sh calls.
#
# This is the one entry point whose composition had no coverage of its own. The
# primitives it reuses are pinned above through `rollback`, but `revoke_all` is
# what `ops/deploy/uninstall.sh` runs as root against a real manifest, and what
# it decides -- which lines stay pending, whether the manifest survives, what it
# prints for the shell to read -- was checkable only by inspecting it against the
# two heredocs it replaced. These cases pin that decision instead.
# --------------------------------------------------------------------------


def _record_for(path: Path, user: str = GRANTEE) -> str:
    """A manifest line describing *path* as it is on disk right now."""
    info = path.stat()
    return host_access.format_traversal_record(user, info.st_dev, info.st_ino, str(path))


def _manifest_with(tmp_path: Path, *lines: str, name: str = "traversal-acl.manifest") -> Path:
    manifest = tmp_path / name
    manifest.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    manifest.chmod(0o600)
    return manifest


def test_revoke_all_reverses_every_grant_and_removes_the_drained_manifest(tmp_path):
    """A manifest with nothing left pending is unlinked, not left empty.

    Uninstall has no baseline to keep, so a drained manifest has no reason to
    exist. Leaving a zero-byte file behind would make the next uninstall's
    existence check find a manifest and walk it for nothing.
    """
    _require_acl_tools()
    storage = tmp_path / "a" / "storage"
    storage.mkdir(parents=True)
    manifest = tmp_path / "traversal-acl.manifest"
    host_access.grant(str(storage), str(tmp_path), GRANTEE, str(manifest), "")
    manifest.chmod(0o600)
    granted = [line for line in manifest.read_text(encoding="utf-8").splitlines() if line]
    assert granted, "the grant recorded nothing to revoke"

    status = host_access.revoke_all("acl", str(manifest), "")

    assert status == 0
    assert _named_entry(storage) is None
    assert not manifest.exists(), "a drained manifest is removed"


def test_revoke_all_keeps_a_line_it_could_not_reverse_and_fails(tmp_path, capsys):
    """The pending lines are rewritten so a second uninstall retries exactly those.

    This is the difference between an uninstall an operator can repeat and one
    that forgets what it could not undo. The entry it *could* take back is gone
    from the host, and only the line it could not is still in the manifest.
    """
    _require_acl_tools()
    ours = tmp_path / "ours"
    theirs = tmp_path / "theirs"
    ours.mkdir()
    theirs.mkdir()
    subprocess.run(["setfacl", "-m", f"u:{GRANTEE}:x", str(ours)], check=True)
    # `rx` is not the `--x` the installer records, so this one cannot be proved ours.
    subprocess.run(["setfacl", "-m", f"u:{GRANTEE}:rx", str(theirs)], check=True)
    pending_line = _record_for(theirs)
    manifest = _manifest_with(tmp_path, _record_for(ours), pending_line)

    status = host_access.revoke_all("acl", str(manifest), "")

    assert status == 1
    assert "could not revoke traversal grant" in capsys.readouterr().err
    assert _named_entry(ours) is None, "the line it could reverse was still reversed"
    assert _named_entry(theirs) is not None, "an entry it cannot prove is ours stays put"
    assert manifest.read_text(encoding="utf-8") == pending_line + "\n"


def test_revoke_all_does_not_count_a_record_it_did_not_have_to_reverse(tmp_path, capsys):
    """The printed count is what changed on the host, not what left the manifest.

    `uninstall.sh` prints this number. A record whose target is already gone has
    to drain -- keeping it would make every later uninstall retry a path that no
    longer exists -- but counting it would tell an operator a grant was removed
    when nothing was touched. The heredoc this replaced reached `continue` for
    exactly this case; collapsing it into the success return is what lost the
    distinction, and the Part 1 review of the split found it.
    """
    manifest = _manifest_with(
        tmp_path, host_access.format_traversal_record(GRANTEE, 2049, 1234, str(tmp_path / "absent"))
    )

    status = host_access.revoke_all("acl", str(manifest), "")

    assert status == 0
    assert capsys.readouterr().out.strip() == "0", "nothing was reversed, so nothing is reported"
    assert not manifest.exists(), "and the record still drains"


def test_revoke_all_refuses_a_manifest_anyone_else_can_write(tmp_path):
    """Custody is checked before the walk, so a tampered manifest revokes nothing.

    The manifest is the only record of what to undo on a host running as root.
    One anybody can write is one anybody can point at a path they want stripped.
    """
    manifest = _manifest_with(tmp_path, host_access.format_traversal_record(GRANTEE, 2049, 1234, "/absent"))
    before = manifest.read_text(encoding="utf-8")
    manifest.chmod(0o666)

    with pytest.raises(SystemExit) as excinfo:
        host_access.revoke_all("acl", str(manifest), "")

    assert "not a protected regular installer-owned file" in str(excinfo.value)
    assert manifest.read_text(encoding="utf-8") == before, "a refused manifest is left alone"


def test_revoke_all_leaves_a_legacy_selinux_record_pending_without_a_storage_root(tmp_path, capsys):
    """Uninstall passes no storage root, so a version-1 record is refused.

    A version-1 SELinux record carries no target of its own, and uninstall has no
    current storage root to offer in its place. Guessing one would relabel a tree
    this installation may never have owned, so the record stays pending and the
    operator is told. Reached before `semanage` is consulted, which is why this
    case needs no SELinux host.
    """
    record = json.dumps({"kind": "selinux", "target": "", "value": "legacy_expression", "version": 1}, sort_keys=True)
    manifest = _manifest_with(tmp_path, record, name="mac-policy.manifest")

    status = host_access.revoke_all("mac", str(manifest), "")

    assert status == 1
    assert "invalid SELinux storage root" in capsys.readouterr().err
    assert manifest.read_text(encoding="utf-8") == record + "\n"


def test_revoke_all_refuses_a_manifest_kind_it_cannot_mean(tmp_path):
    """An unknown kind stops before the manifest is opened at all."""
    manifest = _manifest_with(tmp_path, host_access.format_traversal_record(GRANTEE, 2049, 1234, "/absent"))

    with pytest.raises(SystemExit) as excinfo:
        host_access.revoke_all("acls", str(manifest), "")

    assert "unsupported manifest kind: acls" in str(excinfo.value)
    assert manifest.exists(), "nothing is touched for a kind that cannot be meant"


# --------------------------------------------------------------------------
# The command line the shell functions actually invoke.
# --------------------------------------------------------------------------


def _run_module(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MODULE), *arguments],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def test_the_command_line_grants_records_and_rolls_back(tmp_path):
    _require_acl_tools()
    storage = tmp_path / "a/storage"
    storage.mkdir(parents=True)
    manifest = tmp_path / "manifest"
    log = _protected_log(tmp_path / "transaction")
    for path in (tmp_path / "traversal-acl.before", tmp_path / "mac-policy.before"):
        path.write_text("", encoding="utf-8")

    granted = _run_module("grant", str(storage), str(tmp_path), GRANTEE, str(manifest), str(log))
    assert granted.returncode == 0, granted.stderr
    assert _named_entry(storage) is not None

    rolled_back = _run_module(
        "rollback",
        str(manifest),
        str(tmp_path / "traversal-acl.before"),
        "true",
        str(tmp_path / "mac-policy.manifest"),
        str(tmp_path / "mac-policy.before"),
        "false",
        str(tmp_path),
        str(log),
    )

    assert rolled_back.returncode == 0, rolled_back.stderr
    assert _named_entry(storage) is None
    assert manifest.read_text(encoding="utf-8") == ""


def test_the_command_line_exits_non_zero_when_a_revoke_fails(tmp_path):
    """`rollback_host_access_delta` turns this status into its own return code."""
    _require_acl_tools()
    ours = tmp_path / "ours"
    ours.mkdir()
    subprocess.run(["setfacl", "-m", f"u:{GRANTEE}:rx", str(ours)], check=True)
    manifest = tmp_path / "manifest"
    manifest.write_text(
        host_access.format_traversal_record(GRANTEE, ours.stat().st_dev, ours.stat().st_ino, str(ours)) + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    for path in (tmp_path / "traversal-acl.before", tmp_path / "mac-policy.before"):
        path.write_text("", encoding="utf-8")

    result = _run_module(
        "rollback",
        str(manifest),
        str(tmp_path / "traversal-acl.before"),
        "true",
        str(tmp_path / "mac-policy.manifest"),
        str(tmp_path / "mac-policy.before"),
        "false",
        str(tmp_path),
        "",
    )

    assert result.returncode == 1
    assert "could not roll back traversal grant" in result.stderr


def test_the_command_line_revoke_all_prints_a_bare_count_the_shell_can_compare(tmp_path):
    """`uninstall.sh` reads this stdout with `-eq`, so it must be a number alone.

    The heredoc this replaced printed `removed=N` on the MAC path, and the shell
    function that consumed it had to be rewritten along with it. An integer with
    anything else attached would make `[[ "$removed" -eq 0 ]]` fail as a syntax
    error in a root-executing script, so the format is part of the contract.
    """
    _require_acl_tools()
    storage = tmp_path / "a" / "storage"
    storage.mkdir(parents=True)
    manifest = tmp_path / "traversal-acl.manifest"
    host_access.grant(str(storage), str(tmp_path), GRANTEE, str(manifest), "")
    manifest.chmod(0o600)
    expected = len([line for line in manifest.read_text(encoding="utf-8").splitlines() if line])

    result = _run_module("revoke-all", "acl", str(manifest), "")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().isdigit(), result.stdout
    assert int(result.stdout.strip()) == expected
    assert _named_entry(storage) is None
    assert not manifest.exists()


def test_the_command_line_exits_non_zero_when_revoke_all_leaves_a_line_pending(tmp_path):
    """`revoke_recorded_traversal_grants` turns this status into its own return
    code, which makes uninstall die before it removes anything else."""
    _require_acl_tools()
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    subprocess.run(["setfacl", "-m", f"u:{GRANTEE}:rx", str(theirs)], check=True)
    pending_line = _record_for(theirs)
    manifest = _manifest_with(tmp_path, pending_line)

    result = _run_module("revoke-all", "acl", str(manifest), "")

    assert result.returncode == 1
    assert "could not revoke traversal grant" in result.stderr
    assert manifest.read_text(encoding="utf-8") == pending_line + "\n"
    assert _named_entry(theirs) is not None


@pytest.mark.parametrize(
    "arguments",
    [(), ("grant",), ("record", "log"), ("rollback", "one"), ("revoke", "one", "two")],
)
def test_the_command_line_refuses_an_argument_count_it_cannot_mean(arguments):
    result = _run_module(*arguments)

    assert result.returncode != 0
    assert "usage: host_access.py" in result.stderr
