from __future__ import annotations

import asyncio
import hashlib
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from astinuslab.lab_packages.archive import write_archive
from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.models import (
    AssetReference,
    HostResources,
    LabDefinition,
    LabExperience,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)
from astinuslab.lab_packages.package_service import LabPackageService
from astinuslab.lab_packages.preflight import HostFacts, PackagePreflight, PreflightIssue


def _archive() -> bytes:
    definition = LabDefinition(
        package_id="api-lab",
        version="1.0.0",
        title="API lab",
        creator="Researcher",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=ResourceRecommendations(
            minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
            recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=40 * 1024**3),
        ),
        networks=(LabNetwork(id="isolated", subnet_cidr="192.0.2.0/29"),),
        vms=(
            LabVM(
                id="target",
                role="target",
                image_id="ubuntu-24.04",
                vcpus=2,
                memory_mb=2048,
                disk_gb=10,
                network_ids=("isolated",),
            ),
        ),
        experience=LabExperience(),
    )
    return write_archive(definition, "# API runbook\n")


def test_inspect_endpoint_stages_an_inert_package_until_explicit_commit(package_client, monkeypatch, tmp_path: Path):
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)
    monkeypatch.setattr(
        service,
        "stage_inspection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("multipart upload must not make a second staged copy")
        ),
    )
    monkeypatch.setattr(
        service,
        "cleanup_staging",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("upload must not sweep unjournaled files")),
    )

    response = package_client.post(
        "/api/v1/lab-packages/inspect",
        files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["package_id"] == "api-lab"
    assert body["signature_status"] == "unsigned"
    assert body["runbook"] == "# API runbook\n"
    assert body["inspection_id"].startswith("lpi-")
    assert list((tmp_path / "packages").glob("*.astinuslab"))
    assert not list((tmp_path / "packages").glob(".astinuslab-upload-*"))
    assert not list((tmp_path / "packages").glob(".astinuslab-inspection-*.partial"))
    committed = package_client.post(f"/api/v1/lab-packages/imports/{body['inspection_id']}/commit")
    assert committed.status_code == 201, committed.text
    assert committed.json()["package"]["package_id"] == "api-lab"


def test_inspect_endpoint_lists_descriptor_backed_embedded_assets(package_client, monkeypatch, tmp_path: Path):
    from astinuslab.lab_packages.archive import inspect_archive

    payload = b"descriptor payload" * 32
    digest = hashlib.sha256(payload).hexdigest()
    archive = write_archive(
        inspect_archive(_archive()).definition,
        "# API runbook\n",
        assets={digest: payload},
        allow_legacy_unreferenced_assets=True,
    )
    monkeypatch.setattr("astinuslab.lab_packages.archive.PACKAGE_INLINE_PAYLOAD_BYTES", 8)
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)

    response = package_client.post(
        "/api/v1/lab-packages/inspect",
        files={"file": ("api.astinuslab", archive, "application/octet-stream")},
    )

    assert response.status_code == 200
    assert response.json()["assets"][0]["sha256"] == digest


def test_inspect_endpoint_discloses_external_clean_layer(package_client, monkeypatch, tmp_path: Path):
    from astinuslab.lab_packages.archive import inspect_archive

    base = inspect_archive(_archive()).definition
    layer = AssetReference(
        sha256="d" * 64,
        size_bytes=4096,
        media_type="application/x-qcow2",
        sources=("https://example.test/layers/target.qcow2",),
        license="CC-BY-4.0",
        redistribution_confirmed=False,
    )
    definition = base.model_copy(
        update={
            "reproducibility": "locked",
            "vms": (base.vms[0].model_copy(update={"clean_layer": layer}),),
        }
    )
    archive = write_archive(definition, "# Locked runbook\n")
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)

    response = package_client.post(
        "/api/v1/lab-packages/inspect",
        files={"file": ("locked.astinuslab", archive, "application/octet-stream")},
    )

    assert response.status_code == 200, response.text
    assets = response.json()["assets"]
    assert assets == [
        {
            "path": f"assets/sha256/{layer.sha256}",
            "sha256": layer.sha256,
            "size_bytes": layer.size_bytes,
            "media_type": layer.media_type,
            "embedded": False,
            "sources": list(layer.sources),
            "license": layer.license,
            "redistribution_confirmed": False,
        }
    ]


def test_import_list_and_download_endpoints(package_client, monkeypatch, tmp_path: Path):
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)

    imported = package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
    )
    assert imported.status_code == 201, imported.text
    record = imported.json()["package"]
    assert record["package_id"] == "api-lab"
    assert imported.json()["embedded_assets_cached"] == 0

    listed = package_client.get("/api/v1/lab-packages")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [record["id"]]

    detail = package_client.get(f"/api/v1/lab-packages/{record['id']}")
    assert detail.status_code == 200
    preflight = package_client.post(f"/api/v1/lab-packages/{record['id']}/preflight")
    assert preflight.status_code == 200
    assert {"ready", "required_disk_bytes", "host", "errors", "warnings"} <= preflight.json().keys()
    monkeypatch.setattr(
        "astinuslab.lab_packages.api.preflight_package",
        lambda *_args, **_kwargs: PackagePreflight(
            HostFacts(1, 1024, 1, "x86_64", False),
            1,
            (PreflightIssue("error", "kvm-unavailable", "KVM is unavailable"),),
        ),
    )
    launch = package_client.post(f"/api/v1/lab-packages/{record['id']}/launch")
    assert launch.status_code == 409
    assert launch.json()["detail"]["code"] == "preflight_failed"
    downloaded = package_client.get(f"/api/v1/lab-packages/{record['id']}/download")
    assert downloaded.status_code == 200
    assert downloaded.content == _archive()


def test_launch_requires_the_preflight_warning_acknowledgement(package_client, monkeypatch, tmp_path: Path):
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)
    imported = package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
    )
    record = imported.json()["package"]
    warning = PreflightIssue("warning", "unlocked-package", "This package is not reproducible")
    monkeypatch.setattr(
        "astinuslab.lab_packages.api.preflight_package",
        lambda *_args, **_kwargs: PackagePreflight(
            HostFacts(4, 8192, 60 * 1024**3, "x86_64", True), 1, (warning,), 0, 1
        ),
    )

    preflight = package_client.post(f"/api/v1/lab-packages/{record['id']}/preflight")

    assert preflight.status_code == 200
    assert preflight.json()["warning_token"]
    launch = package_client.post(f"/api/v1/lab-packages/{record['id']}/launch", json={})
    assert launch.status_code == 409
    assert launch.json()["detail"]["code"] == "warnings_unacknowledged"


def test_public_preflight_keeps_actionable_workload_reason(package_client, monkeypatch, tmp_path):
    from astinuslab.playground.workloads import PlaygroundWorkloadValidation

    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)
    monkeypatch.setattr("astinuslab.playground.orchestrator.lookup_playground_image", lambda *a, **k: {"id": "fixture"})
    detail = "workload_id 'missing' not found"
    monkeypatch.setattr(
        "astinuslab.playground.workloads.validate_playground_workload_selection",
        lambda **k: PlaygroundWorkloadValidation(ok=False, detail=detail),
    )
    imported = package_client.post(
        "/api/v1/lab-packages/import", files={"file": ("api.astinuslab", _archive(), "application/octet-stream")}
    )
    assert imported.status_code == 201
    response = package_client.post(f"/api/v1/lab-packages/{imported.json()['package']['id']}/preflight")
    assert response.status_code == 200
    issue = next(item for item in response.json()["errors"] if item["code"] == "workload-incompatible")
    assert detail in issue["message"] and "target" in issue["message"]


def test_launch_returns_durable_operation_handle_and_dispatches_after_commit(
    package_client, monkeypatch, tmp_path: Path
):
    from astinuslab.lab_packages.launch_service import LaunchOperation

    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)
    monkeypatch.setattr(
        "astinuslab.lab_packages.api.preflight_package",
        lambda *_args, **_kwargs: PackagePreflight(HostFacts(4, 8192, 60 * 1024**3, "x86_64", True), 1, ()),
    )
    monkeypatch.setattr("astinuslab.lab_packages.api.validate_warning_consent_token", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "astinuslab.lab_packages.api.launch_package",
        lambda *_args, **_kwargs: LaunchOperation("lpi-operation", "pg-operation", True),
    )
    dispatched: list[str] = []
    monkeypatch.setattr("astinuslab.lab_packages.api.dispatch_instance_after_commit", dispatched.append)
    imported = package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
    )

    response = package_client.post(
        f"/api/v1/lab-packages/{imported.json()['package']['id']}/launch",
        json={"idempotency_key": "operation-key", "acknowledged_warning_token": "ack"},
    )

    assert response.status_code == 202, response.text
    assert response.json()["id"] == "lpi-operation"
    assert response.json()["session_id"] == "pg-operation"
    assert dispatched == ["lpi-operation"]


def test_launch_idempotency_lookup_precedes_expired_consent(
    package_client, in_memory_session_scope, monkeypatch, tmp_path: Path
):
    from astinuslab.db.models import LabInstance
    from astinuslab.lab_packages.package_catalog import LabPackageRepository

    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)
    imported = package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
    )
    record_id = imported.json()["package"]["id"]
    with in_memory_session_scope() as session:
        record = LabPackageRepository(session, archive_root=service.package_root).get_by_id(record_id)
        assert record is not None
        session.add(
            LabInstance(
                id="lpi-idempotent",
                package_record_id=record.id,
                playground_session_id=None,
                idempotency_key="retry-key",
                state="failed",
                error_summary="prior launch failed",
            )
        )
    monkeypatch.setattr(
        "astinuslab.lab_packages.api.preflight_package",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("retry must not preflight")),
    )

    response = package_client.post(
        f"/api/v1/lab-packages/{record_id}/launch",
        json={"idempotency_key": "retry-key", "acknowledged_warning_token": "expired"},
    )

    assert response.status_code == 202, response.text
    assert response.json()["id"] == "lpi-idempotent"
    assert response.json()["state"] == "failed"


def test_download_rejects_tampered_retained_archive(package_client, monkeypatch, tmp_path: Path):
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)

    imported = package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
    )
    assert imported.status_code == 201, imported.text
    record = imported.json()["package"]
    retained = next(service.package_root.glob("*.astinuslab"))
    original = retained.read_bytes()
    retained.write_bytes(b"x" * len(original))

    response = package_client.get(f"/api/v1/lab-packages/{record['id']}/download")

    assert response.status_code == 410


@pytest.mark.parametrize("fault", [None, "replace", "tamper", "missing", "parse", "identity"])
def test_experience_uses_verified_descriptor_and_releases_custody(
    package_client, in_memory_session_scope, monkeypatch, tmp_path: Path, fault
):
    from astinuslab.db.models import LabDraft, LabPackageInspection, LabStorageLease, PlaygroundSession
    from astinuslab.lab_packages import api as package_api
    from astinuslab.lab_packages.archive import PackageArchiveError, inspect_archive

    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr(package_api, "default_package_service", lambda: service)
    definition = inspect_archive(_archive()).definition
    runbook = "# Kept instructions\n<script>not executable</script>"
    payload = write_archive(definition, runbook)
    imported = package_client.post(
        "/api/v1/lab-packages/import", files={"file": ("lab.astinuslab", payload, "application/octet-stream")}
    )
    assert imported.status_code == 201
    record = imported.json()["package"]
    retained = next(service.package_root.glob("*.astinuslab"))
    with in_memory_session_scope() as s:
        inspections_before = s.query(LabPackageInspection).count()
        if fault == "identity":
            from sqlalchemy import update

            from astinuslab.db.models import LabPackage

            # Simulate corrupted durable binding, bypassing the ORM's ordinary
            # immutable-record guard to exercise the read refusal.
            s.execute(update(LabPackage).where(LabPackage.id == record["id"]).values(package_id="other-package"))
    handles = []
    original_open = package_api._open_verified_archive

    def opened(*args):
        handle = original_open(*args)
        handles.append(handle)
        with in_memory_session_scope() as s:
            assert s.query(LabStorageLease).filter_by(resource_id=record["id"]).count() == 1
        if fault == "replace":
            replacement = retained.with_suffix(".replacement")
            replacement.write_bytes(b"unverified replacement")
            replacement.replace(retained)
        if fault == "parse":

            def refuse(*args, **kwargs):
                raise PackageArchiveError("injected parse failure")

            monkeypatch.setattr("astinuslab.lab_packages.package_service.inspect_archive", refuse)
        return handle

    monkeypatch.setattr(package_api, "_open_verified_archive", opened)
    if fault == "tamper":
        retained.write_bytes(b"x" * len(payload))
    if fault == "missing":
        retained.unlink()
    response = package_client.get(f"/api/v1/lab-packages/{record['id']}/experience")
    if fault in {"tamper", "missing", "parse", "identity"}:
        assert response.status_code == 410, response.text
    else:
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["runbook"] == runbook
        assert body["package_record_id"] == record["id"]
        assert body["archive_sha256"] == record["archive_sha256"]
        assert body["manifest_sha256"] == record["manifest_sha256"]
        assert body["signature_status"] == record["signature_status"]
        assert body["experience"] == definition.experience.model_dump(mode="json")
        assert body["resources"] == definition.resources.model_dump(mode="json")
        if fault is None:
            assert package_client.get(f"/api/v1/lab-packages/{record['id']}/experience").json() == body
    assert all(handle.closed for handle in handles)
    with in_memory_session_scope() as s:
        assert s.query(LabStorageLease).count() == 0
        assert s.query(LabDraft).count() == 0
        assert s.query(LabPackageInspection).count() == inspections_before
        assert s.query(PlaygroundSession).count() == 0
    assert package_client.get("/api/v1/lab-packages/missing/experience").status_code == 404


def test_invalid_package_import_returns_client_error(package_client, monkeypatch, tmp_path: Path):
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)

    response = package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("bad.astinuslab", b"not a package", "application/octet-stream")},
    )

    assert response.status_code == 400


def test_multipart_rejects_spooled_oversize_before_reserving_managed_storage(
    package_client, monkeypatch, tmp_path: Path
):
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    service.archive_max_bytes = 1
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)
    monkeypatch.setattr(
        service,
        "reserve_inspection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("oversized spool must not reserve a second file")
        ),
    )

    response = package_client.post(
        "/api/v1/lab-packages/inspect",
        files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
    )

    assert response.status_code == 413
    assert not list(service.package_root.glob(".astinuslab-*"))


def test_delete_and_gc_remove_a_retained_inert_package(package_client, monkeypatch, tmp_path: Path):
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)
    imported = package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
    )
    assert imported.status_code == 201, imported.text
    record_id = imported.json()["package"]["id"]

    deleted = package_client.delete(f"/api/v1/lab-packages/{record_id}")
    assert deleted.status_code == 204, deleted.text
    assert package_client.get(f"/api/v1/lab-packages/{record_id}").status_code == 404
    gc = package_client.post("/api/v1/lab-packages/gc")
    assert gc.status_code == 200, gc.text


def test_prepare_endpoint_returns_durable_handle_and_is_idempotent(package_client, monkeypatch, tmp_path: Path):
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)
    dispatched: list[str] = []
    monkeypatch.setattr("astinuslab.lab_packages.api.dispatch_prepare_after_commit", dispatched.append)

    imported = package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
    )
    assert imported.status_code == 201, imported.text
    record_id = imported.json()["package"]["id"]

    first = package_client.post(
        f"/api/v1/lab-packages/{record_id}/prepare",
        json={"idempotency_key": "api-prepare"},
    )
    assert first.status_code == 202, first.text
    assert first.json()["state"] == "reserved"
    assert dispatched == [first.json()["id"]]

    retry = package_client.post(
        f"/api/v1/lab-packages/{record_id}/prepare",
        json={"idempotency_key": "api-prepare"},
    )
    assert retry.status_code == 202, retry.text
    assert retry.json()["id"] == first.json()["id"]
    status = package_client.get(f"/api/v1/lab-packages/{record_id}/prepare/{first.json()['id']}")
    assert status.status_code == 200, status.text


def test_export_endpoint_returns_canonical_package_for_a_playground_session(package_client, monkeypatch):
    archive = _archive()
    from astinuslab.lab_packages.archive import inspect_archive

    definition = inspect_archive(archive).definition
    monkeypatch.setattr(
        "astinuslab.lab_packages.api.definition_from_session",
        lambda *_args, **_kwargs: definition,
    )

    response = package_client.post(
        "/api/v1/lab-packages/export",
        json={
            "session_id": "pg-export",
            "package_id": definition.package_id,
            "version": definition.version,
            "title": definition.title,
            "description": definition.description,
            "creator": definition.creator,
            "license": definition.license,
            "resources": definition.resources.model_dump(mode="json"),
            "runbook": "# API runbook\n",
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.astinuslab.lab+zip"
    exported = inspect_archive(response.content)
    assert exported.definition == definition
    assert exported.runbook == "# API runbook\n"


def test_export_inspect_import_download_round_trip(package_client, monkeypatch, tmp_path: Path):
    from astinuslab.lab_packages.archive import inspect_archive

    archive = _archive()
    definition = inspect_archive(archive).definition
    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)
    monkeypatch.setattr(
        "astinuslab.lab_packages.api.definition_from_session",
        lambda *_args, **_kwargs: definition,
    )

    exported = package_client.post(
        "/api/v1/lab-packages/export",
        json={
            "session_id": "pg-round-trip",
            "package_id": definition.package_id,
            "version": definition.version,
            "title": definition.title,
            "description": definition.description,
            "creator": definition.creator,
            "license": definition.license,
            "resources": definition.resources.model_dump(mode="json"),
            "runbook": "# API runbook\n",
        },
    )
    assert exported.status_code == 200, exported.text

    inspected = package_client.post(
        "/api/v1/lab-packages/inspect",
        files={"file": ("round-trip.astinuslab", exported.content, "application/octet-stream")},
    )
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["package_id"] == definition.package_id

    imported = package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("round-trip.astinuslab", exported.content, "application/octet-stream")},
    )
    assert imported.status_code == 201, imported.text
    record_id = imported.json()["package"]["id"]

    downloaded = package_client.get(f"/api/v1/lab-packages/{record_id}/download")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == exported.content


def test_activity_route_precedes_package_detail_and_returns_an_empty_envelope(package_client):
    response = package_client.get("/api/v1/lab-packages/activity")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "prepares": [],
        "verifications": [],
        "instances": [],
        "recent_terminal_verification_count": 0,
    }


def test_activity_lists_current_work_and_verified_exports_in_stable_order_with_status_parity(
    package_client, in_memory_session_scope, monkeypatch, tmp_path: Path
):
    from astinuslab.db.models import (
        LabDraft,
        LabInstance,
        LabOperationOutbox,
        LabPackageAsset,
        LabPrepareAttempt,
        LabVerificationAttempt,
    )
    from astinuslab.lab_packages.archive import inspect_archive

    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)
    imported = package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
    )
    assert imported.status_code == 201, imported.text
    record_id = imported.json()["package"]["id"]

    tied_at = datetime(2026, 9, 6, 10, 0, 0)
    newest_at = tied_at + timedelta(minutes=1)
    oldest_at = tied_at - timedelta(minutes=1)
    definition_json = inspect_archive(_archive()).definition.model_dump_json()
    with in_memory_session_scope() as session:
        session.add(
            LabPackageAsset(
                id="lpa-asset",
                package_record_id=record_id,
                logical_path="assets/sha256/" + "a" * 64,
                role="boot-layer",
                expected_sha256="a" * 64,
                expected_size_bytes=4096,
                media_type="application/x-qcow2",
                origin="source",
                source_json='["https://example.test/layer.qcow2"]',
                prepare_state="resolving",
                prepare_attempt_id="lprep-newest",
                prepared_origin="source",
            )
        )
        session.add_all(
            [
                LabPrepareAttempt(
                    id="lprep-newest",
                    package_record_id=record_id,
                    definition_sha256="a" * 64,
                    state="running",
                    idempotency_key="prepare-newest",
                    progress_percent=45,
                    network_opt_in=True,
                    updated_at=newest_at,
                ),
                LabPrepareAttempt(
                    id="lprep-a-tied",
                    package_record_id=record_id,
                    definition_sha256="a" * 64,
                    state="reserved",
                    idempotency_key="prepare-a-tied",
                    progress_percent=0,
                    updated_at=tied_at,
                ),
                LabPrepareAttempt(
                    id="lprep-b-tied",
                    package_record_id=record_id,
                    definition_sha256="a" * 64,
                    state="running",
                    idempotency_key="prepare-b-tied",
                    progress_percent=10,
                    updated_at=tied_at,
                ),
                *[
                    LabPrepareAttempt(
                        id=f"lprep-terminal-{state}",
                        package_record_id=record_id,
                        definition_sha256="a" * 64,
                        state=state,
                        idempotency_key=f"prepare-terminal-{state}",
                        progress_percent=100,
                        result_json='{"secret":"must-not-leak"}',
                        updated_at=newest_at + timedelta(minutes=1),
                    )
                    for state in ("completed", "failed", "cancelled")
                ],
            ]
        )
        for index, (attempt_id, state, updated_at) in enumerate(
            [
                ("lverify-newest", "running", newest_at),
                ("lverify-a-tied", "reserved", tied_at),
                ("lverify-b-tied", "running", tied_at),
                ("lverify-terminal-verified", "verified", newest_at + timedelta(minutes=1)),
                ("lverify-terminal-failed", "failed", newest_at + timedelta(minutes=1)),
                ("lverify-terminal-cancelled", "cancelled", newest_at + timedelta(minutes=1)),
                *[(f"lverify-old-{index:02d}", "failed", oldest_at) for index in range(19)],
            ]
        ):
            draft_id = f"ld-activity-{index}"
            session.add(
                LabDraft(
                    id=draft_id,
                    definition_json=definition_json,
                    definition_sha256=f"{index + 1:064x}",
                    runbook="# Activity test\n",
                    supporting_files_json="{}",
                )
            )
            session.add(
                LabVerificationAttempt(
                    id=attempt_id,
                    draft_id=draft_id,
                    definition_sha256=f"{index + 1:064x}",
                    state=state,
                    progress_percent=55 if state == "running" else 0,
                    result_json='{"secret":"must-not-leak"}',
                    error_summary=(
                        "checkpoint_failed: /private/runner TOKEN_SECRET_DO_NOT_EXPOSE"
                        if attempt_id == "lverify-terminal-failed"
                        else "unknown: /private/runner TOKEN_SECRET_DO_NOT_EXPOSE"
                    ),
                    updated_at=updated_at,
                )
            )
        instance_states = (
            "registered",
            "creating",
            "ready",
            "failed",
            "cancel_requested",
            "rollback_pending",
            "cleanup_incomplete",
            "resetting",
            "destroying",
            "destroyed",
        )
        for state in instance_states:
            updated_at = (
                newest_at if state == "ready" else tied_at if state in {"failed", "cleanup_incomplete"} else oldest_at
            )
            session.add(
                LabInstance(
                    id=f"linstance-{state}",
                    package_record_id=record_id,
                    playground_session_id=None,
                    idempotency_key=f"instance-{state}",
                    state=state,
                    error_summary="action required" if state in {"failed", "cleanup_incomplete"} else None,
                    updated_at=updated_at,
                )
            )

    def durable_snapshot():
        with in_memory_session_scope() as session:
            return {
                "prepares": session.query(LabPrepareAttempt.id, LabPrepareAttempt.state, LabPrepareAttempt.updated_at)
                .order_by(LabPrepareAttempt.id)
                .all(),
                "verifications": session.query(
                    LabVerificationAttempt.id, LabVerificationAttempt.state, LabVerificationAttempt.updated_at
                )
                .order_by(LabVerificationAttempt.id)
                .all(),
                "instances": session.query(LabInstance.id, LabInstance.state, LabInstance.updated_at)
                .order_by(LabInstance.id)
                .all(),
                "outbox_count": session.query(LabOperationOutbox).count(),
            }

    before = durable_snapshot()
    monkeypatch.setattr(
        "astinuslab.lab_packages.api.preflight_package",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("activity must not inspect host state")),
    )
    monkeypatch.setattr(
        "astinuslab.lab_packages.api.default_package_service",
        lambda: (_ for _ in ()).throw(AssertionError("activity must not open package storage")),
    )
    for dispatcher in (
        "dispatch_prepare_after_commit",
        "dispatch_verification_after_commit",
        "dispatch_instance_after_commit",
        "dispatch_instance_action_after_commit",
    ):
        monkeypatch.setattr(
            f"astinuslab.lab_packages.api.{dispatcher}",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("activity must not dispatch background work")
            ),
        )

    response = package_client.get("/api/v1/lab-packages/activity")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"prepares", "verifications", "instances", "recent_terminal_verification_count"}
    assert body["recent_terminal_verification_count"] == 20
    assert [item["id"] for item in body["prepares"]] == ["lprep-newest", "lprep-a-tied", "lprep-b-tied"]
    assert [item["id"] for item in body["verifications"]] == [
        "lverify-terminal-cancelled",
        "lverify-terminal-failed",
        "lverify-terminal-verified",
        "lverify-newest",
        "lverify-a-tied",
        "lverify-b-tied",
        *[f"lverify-old-{index:02d}" for index in range(18)],
    ]
    assert body["verifications"][0]["error_code"] == "verification_cancelled"
    assert body["verifications"][0]["error_summary"] == "The clean-run verification was cancelled."
    assert body["verifications"][1]["error_code"] == "checkpoint_failed"
    assert body["verifications"][1]["error_summary"] == (
        "A clean-run checkpoint did not pass. Review the checkpoint definition and guest state before rebuilding the lab."
    )
    for item in body["verifications"][2:6]:
        assert item["error_code"] is None
        assert item["error_summary"] is None
    assert body["verifications"][-1]["error_code"] == "verification_failed"
    assert body["verifications"][-1]["error_summary"] == (
        "The clean-run verification failed. Check runner logs using this verification ID."
    )
    assert "TOKEN_SECRET_DO_NOT_EXPOSE" not in response.text
    assert "/private/runner" not in response.text
    assert [item["id"] for item in body["instances"]] == [
        "linstance-ready",
        "linstance-cleanup_incomplete",
        "linstance-failed",
        "linstance-cancel_requested",
        "linstance-creating",
        "linstance-destroying",
        "linstance-registered",
        "linstance-resetting",
        "linstance-rollback_pending",
    ]
    assert set(body["prepares"][0]) == {
        "id",
        "package_record_id",
        "state",
        "progress_percent",
        "error_summary",
        "network_opt_in",
        "assets",
    }
    assert set(body["verifications"][0]) == {
        "id",
        "draft_id",
        "state",
        "progress_percent",
        "authored_session_id",
        "error_code",
        "error_summary",
        "package_id",
        "package_version",
        "package_title",
        "attestation_expires_at",
        "attestation_status",
    }
    assert set(body["instances"][0]) == {
        "id",
        "package_record_id",
        "session_id",
        "state",
        "error_summary",
    }
    assert body["prepares"] == [
        package_client.get(f"/api/v1/lab-packages/prepare/{item['id']}").json() for item in body["prepares"]
    ]
    assert body["verifications"] == [
        package_client.get(f"/api/v1/lab-packages/verifications/{item['id']}").json() for item in body["verifications"]
    ]
    assert body["instances"] == [
        package_client.get(f"/api/v1/lab-packages/operations/{item['id']}").json() for item in body["instances"]
    ]
    assert durable_snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "boundary", "expected_status"),
    [
        ("inspect", "finalize_staged_upload", 200),
        ("import", "finalize_staged_upload", 201),
        ("import", "commit_inspection", 201),
    ],
)
async def test_package_upload_does_not_block_event_loop(
    in_memory_session_scope, monkeypatch, tmp_path: Path, endpoint, boundary, expected_status
):
    from astinuslab import db as db_mod
    from astinuslab.lab_packages.api import router

    app = FastAPI()
    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)
    app.include_router(router)

    @app.get("/health")
    async def health():
        return {"ok": True}

    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)
    real_boundary = getattr(service, boundary)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    boundary_threads = []

    def blocked_boundary(*args, **kwargs):
        boundary_threads.append(threading.get_ident())
        entered.set()
        release.wait(timeout=10)  # Independent safety release also makes the old blocking handler fail safely.
        finished.set()
        return real_boundary(*args, **kwargs)

    monkeypatch.setattr(service, boundary, blocked_boundary)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        upload = asyncio.create_task(
            client.post(
                f"/api/v1/lab-packages/{endpoint}",
                files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
            )
        )
        try:

            async def wait_for_boundary():
                while not entered.is_set() and not upload.done():
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_boundary(), timeout=15)
            assert entered.is_set(), "Upload failed before the blocking boundary"
            assert (await asyncio.wait_for(client.get("/health"), timeout=5)).status_code == 200
            assert not finished.is_set(), "The event loop waited for the blocking upload operation"
            assert boundary_threads == [boundary_threads[0]]
            assert boundary_threads[0] != loop_thread
        finally:
            release.set()
            response = await asyncio.wait_for(upload, timeout=15)
        assert response.status_code == expected_status, response.text


@pytest.mark.asyncio
async def test_session_lifetime_and_thread_identity_within_single_operation(
    in_memory_session_scope, monkeypatch, tmp_path: Path
):
    from astinuslab import db as db_mod
    from astinuslab.lab_packages.api import router

    app = FastAPI()
    base_scope = in_memory_session_scope
    session_events: list[tuple[str, int]] = []

    @contextmanager
    def tracked_session_scope():
        tid = threading.get_ident()
        session_events.append(("session_create", tid))
        try:
            with base_scope() as session:
                orig_commit = session.commit

                def wrapped_commit():
                    session_events.append(("db_commit", threading.get_ident()))
                    return orig_commit()

                monkeypatch.setattr(session, "commit", wrapped_commit)
                yield session
        finally:
            session_events.append(("session_close", threading.get_ident()))

    monkeypatch.setattr(db_mod, "session_scope", tracked_session_scope)
    app.include_router(router)

    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)

    event_loop_thread_id = threading.get_ident()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. /inspect positive thread identity
        session_events.clear()
        response = await client.post(
            "/api/v1/lab-packages/inspect",
            files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
        )
        assert response.status_code == 200
        inspect_events = list(session_events)
        actions = [ev[0] for ev in inspect_events]
        assert "session_create" in actions
        assert "db_commit" in actions
        assert "session_close" in actions
        tids = [ev[1] for ev in inspect_events]
        assert len(set(tids)) == 1, f"Session events spanned multiple threads: {inspect_events}"
        assert tids[0] != event_loop_thread_id, "Session operations executed on event loop thread!"

        # 2. /import positive thread identity
        session_events.clear()
        response = await client.post(
            "/api/v1/lab-packages/import",
            files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
        )
        assert response.status_code == 201
        import_events = list(session_events)
        actions = [ev[0] for ev in import_events]
        assert "session_create" in actions
        assert "db_commit" in actions
        assert "session_close" in actions
        tids = [ev[1] for ev in import_events]
        assert len(set(tids)) == 1, f"Session events spanned multiple threads: {import_events}"
        assert tids[0] != event_loop_thread_id, "Session operations executed on event loop thread!"


def test_upload_inspection_journal_cleanup_under_injected_failure(package_client, monkeypatch, tmp_path: Path):
    from astinuslab.db import session_scope
    from astinuslab.db.models import LabPackageInspection

    service = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: service)

    def failing_finalize(*_args, **_kwargs):
        raise OSError("injected disk failure during inspection")

    monkeypatch.setattr(service, "finalize_staged_upload", failing_finalize)

    response = package_client.post(
        "/api/v1/lab-packages/inspect",
        files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
    )

    assert response.status_code == 400
    assert "could not stage package upload" in response.text

    with session_scope() as session:
        inspections = session.query(LabPackageInspection).all()
        assert len(inspections) == 1
        assert inspections[0].state == "failed"
        assert not service.inspection_partial_path(inspections[0]).exists()
        assert not Path(inspections[0].staged_path).exists()
