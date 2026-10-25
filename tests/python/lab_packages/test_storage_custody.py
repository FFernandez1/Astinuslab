"""Policy validation and failure-state checks; actual ACL identities live in tooling."""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from astinuslab.db.models import LabPackage, LabPackageInspection
from astinuslab.lab_packages import package_service
from astinuslab.lab_packages import storage_custody as custody
from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.prepare_service import create_prepare, run_prepare
from tests.python._support.lab_packages import service_package as _package


@pytest.fixture()
def installed_policy(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    root.mkdir()
    root.chmod(0o3770)
    store = root / "lab-packages"
    store.mkdir()
    marker = tmp_path / "runtime-identities"
    marker.write_bytes(custody.IDENTITIES)
    monkeypatch.setattr(custody, "IDENTITIES_PATH", marker)
    monkeypatch.setattr(custody, "configured_storage_root", lambda: root)
    ids = {"astinuslab-api": os.getuid(), "astinuslab-runner": os.getuid() + 1, "astinuslab-analysis": os.getuid() + 2}
    monkeypatch.setattr(custody.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=ids[name]))
    policy = {
        "schema_version": "astinuslab-acl-policy/v1",
        "allowed_roots": [str(root)],
        "allowed_callers": ["astinuslab-runner"],
    }
    # This fixture simulates protected policy lookup; it does not claim a root
    # trust boundary. The unsafe-ancestor test exercises the real refusal.
    monkeypatch.setattr(
        custody, "_protected_bytes", lambda path: marker.read_bytes() if path == marker else json.dumps(policy).encode()
    )
    return root, store, marker, policy, ids


def test_policy_requires_exact_marker_authorized_root_and_distinct_roles(installed_policy):
    root, store, marker, policy, ids = installed_policy
    actual_policy = custody.policy_for(store)
    assert actual_policy is not None
    assert actual_policy.writers == frozenset((ids["astinuslab-api"], ids["astinuslab-runner"]))
    marker.write_bytes(b"mode=single\n")
    with pytest.raises(OSError, match="identity marker"):
        custody.policy_for(store)
    marker.write_bytes(custody.IDENTITIES)
    policy["allowed_roots"] = [str(root.parent)]
    with pytest.raises(OSError, match="installed package custody policy"):
        custody.policy_for(store)
    policy["allowed_roots"] = [str(root)]
    ids["astinuslab-analysis"] = ids["astinuslab-runner"]
    with pytest.raises(OSError, match="installed package custody policy"):
        custody.policy_for(store)


def test_policy_requires_sticky_parent_and_rejects_symlink(installed_policy):
    root, store, _, _, _ = installed_policy
    root.chmod(0o2770)
    with pytest.raises(OSError, match="quiesced split custody migration"):
        custody.policy_for(store)
    root.chmod(0o3770)
    link = store / "link"
    link.symlink_to(root)
    with pytest.raises(OSError, match="symbolic link"):
        custody.policy_for(link)


def test_uninstalled_and_unrelated_paths_keep_private_fallback(installed_policy):
    root, store, marker, _, _ = installed_policy
    assert custody.policy_for(root / "dumps") is None
    marker.unlink()
    assert custody.policy_for(store) is None
    custody.secure_directory(store)
    assert store.stat().st_mode & 0o777 == 0o700


def test_policy_file_under_unprotected_ancestor_is_refused(tmp_path):
    marker = tmp_path / "policy"
    marker.write_bytes(b"untrusted")
    with pytest.raises(OSError, match="unprotected ancestor"):
        custody._protected_bytes(marker)


def test_custody_permission_failure_leaves_inspection_failed(in_memory_session_scope, tmp_path, monkeypatch):
    source, _ = _package(tmp_path)
    service = package_service.LabPackageService(
        package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content")
    )
    real = package_service.secure_file

    def refuse_upload(fd, path):
        if path.name.endswith(".partial"):
            raise PermissionError("simulated denied ACL setup")
        real(fd, path)

    monkeypatch.setattr(package_service, "secure_file", refuse_upload)
    with in_memory_session_scope() as session:
        with pytest.raises(package_service.PackageImportError, match="quiesced split package-custody migration"):
            service.import_path(source, session)
        assert session.scalar(select(LabPackage)) is None
        rows = session.scalars(select(LabPackageInspection)).all()
        assert rows and all(row.state == "failed" for row in rows)


@pytest.mark.parametrize(
    ("failure_errno", "transient", "expected_error"),
    [
        (errno.EACCES, False, "quiesced split package-custody migration"),
        (errno.EIO, True, "content store rejected asset"),
    ],
    ids=("permission-denied", "transient-read-error"),
)
def test_prepare_read_failure_is_actionable_and_never_ready(
    in_memory_session_scope, tmp_path, monkeypatch, failure_errno, transient, expected_error
):
    source, content = _package(tmp_path)
    service = package_service.LabPackageService(
        package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content")
    )
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        attempt = create_prepare(session, record=record, service=service, idempotency_key="custody-denied")
        session.commit()

        digest = hashlib.sha256(content).hexdigest()
        cache_path = service.content_store.root / "sha256" / digest[:2] / digest
        real_open = os.open
        denied: list[Path] = []

        def refuse(path, *args, **kwargs):
            if path == cache_path and not (transient and denied):
                denied.append(path)
                raise OSError(failure_errno, "simulated failed cache read", str(path))
            return real_open(path, *args, **kwargs)

        # Exercise the actual read used by immutable cache verification, not
        # path_for(), which a valid retained read no longer needs.
        monkeypatch.setattr(os, "open", refuse)
        completed = run_prepare(session, attempt.id, service=service)
        assert denied == [cache_path]
        assert completed.state == "failed"
        assert all(asset.state == "failed" for asset in completed.assets)
        assert all(
            asset.error_summary is not None and expected_error in asset.error_summary for asset in completed.assets
        )
        # Failure did not delete the verified cache bytes.
        assert cache_path.read_bytes() == content


def test_default_prepare_constructor_denial_preserves_unclaimed_intent(in_memory_session_scope, tmp_path, monkeypatch):
    from astinuslab.db.models import LabOperationOutbox, LabPrepareAttempt, LabStorageLease
    from astinuslab.lab_packages import prepare_service
    from astinuslab.lab_packages.content_store import ContentStoreError

    source, content = _package(tmp_path)
    service = package_service.LabPackageService(
        package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content")
    )
    with in_memory_session_scope() as session:
        record = service.import_path(source, session).record
        attempt = create_prepare(session, record=record, service=service, idempotency_key="constructor-denied")
        session.commit()

        def denied_constructor():
            try:
                raise PermissionError("simulated default storage denial")
            except PermissionError as exc:
                raise ContentStoreError("could not secure content store root") from exc

        monkeypatch.setattr(prepare_service, "LabPackageService", denied_constructor)
        with pytest.raises(ContentStoreError):
            run_prepare(session, attempt.id)
        # A storage failure before package custody cannot authorize any claim
        # or cleanup write. Keep intent available after permissions are repaired.
        reserved = session.get(LabPrepareAttempt, attempt.id)
        assert reserved.state == "reserved"
        assert reserved.execution_token is None
        assert reserved.error_summary is None
        assert session.query(LabOperationOutbox).filter_by(operation_id=attempt.id).one().state == "pending"
        assert session.query(LabStorageLease).filter_by(owner_id=attempt.id).count() > 0
        digest = hashlib.sha256(content).hexdigest()
        assert (service.content_store.root / "sha256" / digest[:2] / digest).read_bytes() == content
        assert source.read_bytes()


def test_installed_writer_policy_replaces_inherited_access_on_files_and_directories(installed_policy):
    import subprocess

    _, store, _, _, ids = installed_policy
    child = store / "writer-data"
    child.mkdir(mode=0o755)
    payload = child / "payload"
    payload.write_bytes(b"retained package bytes")
    payload.chmod(0o644)

    custody.secure_directory(child)
    with payload.open("rb") as stream:
        custody.secure_file(stream.fileno(), payload)

    def acl(path):
        return set(subprocess.check_output(["getfacl", "-cnp", str(path)], text=True).splitlines())

    file_acl, directory_acl = acl(payload), acl(child)
    for role in ("astinuslab-api", "astinuslab-runner"):
        assert f"user:{ids[role]}:rw-" in file_acl
        assert f"user:{ids[role]}:rwx" in directory_acl
        assert f"default:user:{ids[role]}:rwx" in directory_acl
    assert {"group::---", "other::---"} <= file_acl
    assert {"group::---", "other::---", "default:group::---", "default:other::---"} <= directory_acl
    assert not any(line.startswith(f"user:{ids['astinuslab-analysis']}:") for line in file_acl | directory_acl)
    assert payload.read_bytes() == b"retained package bytes"


def test_custody_refuses_a_hard_link_before_changing_permissions(tmp_path):
    payload = tmp_path / "payload"
    payload.write_bytes(b"retained")
    payload.chmod(0o640)
    alias = tmp_path / "alias"
    alias.hardlink_to(payload)

    with payload.open("rb") as stream, pytest.raises(OSError, match="single-link regular file"):
        custody.secure_file(stream.fileno(), payload)

    assert payload.stat().st_mode & 0o777 == 0o640
    assert alias.read_bytes() == b"retained"


def test_prepare_package_lock_uses_existing_writer_acl_and_excludes_analysis(installed_policy):
    import subprocess

    from astinuslab.lab_packages.prepare_authority import try_package_lock

    _, store, _, _, ids = installed_policy
    root = store / ".locks"
    root.mkdir()
    custody.secure_directory(root)
    path = root / "prepare-package-synthetic.lock"
    with try_package_lock(path, "synthetic-package"):
        acl = set(subprocess.check_output(["getfacl", "-cnp", str(path)], text=True).splitlines())
        for role in ("astinuslab-api", "astinuslab-runner"):
            assert f"user:{ids[role]}:rw-" in acl
        assert {"group::---", "other::---"} <= acl
        assert not any(line.startswith(f"user:{ids['astinuslab-analysis']}:") for line in acl)
