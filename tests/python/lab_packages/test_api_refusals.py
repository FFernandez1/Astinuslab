"""Refusal contract for the package HTTP surface.

Every route here can be reached by an operator with a crafted request, and the
status code is part of the contract the UI depends on: 400 for a package the
reader rejects, 409 for a state conflict the caller can resolve, 410 for a
staged inspection that has expired, 413 for an upload past the configured
ceiling. Locked export in particular must refuse anything that was not covered
by a completed clean-run verification, and must say which precondition failed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.models import (
    HostResources,
    LabDefinition,
    LabExperience,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)
from astinuslab.lab_packages.package_service import (
    LabPackageService,
    PackageImportConflictError,
    PackageImportError,
    PackageInspectionExpiredError,
)

pytestmark = pytest.mark.unit

_RESOURCES = ResourceRecommendations(
    minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
    recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=40 * 1024**3),
)


def _definition() -> LabDefinition:
    return LabDefinition(
        package_id="refusal-lab",
        version="1.0.0",
        title="Refusal lab",
        creator="Researcher",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=_RESOURCES,
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


def _archive() -> bytes:
    from astinuslab.lab_packages.archive import write_archive

    return write_archive(_definition(), "# Refusal runbook\n")


@pytest.fixture()
def service(package_client, monkeypatch, tmp_path: Path) -> LabPackageService:
    created = LabPackageService(package_root=tmp_path / "packages", content_store=ContentStore(tmp_path / "content"))
    monkeypatch.setattr("astinuslab.lab_packages.api.default_package_service", lambda: created)
    return created


def _export_body(**overrides) -> dict:
    body = {
        "package_id": "refusal-lab",
        "version": "1.0.0",
        "title": "Refusal lab",
        "creator": "Researcher",
        "license": "CC-BY-4.0",
        "runbook": "# Refusal runbook\n",
        "resources": {
            "minimum": {"cpu_threads": 2, "memory_mb": 4096, "free_disk_bytes": 40 * 1024**3},
            "recommended": {"cpu_threads": 4, "memory_mb": 8192, "free_disk_bytes": 40 * 1024**3},
        },
    }
    body.update(overrides)
    return body


# --- upload staging ------------------------------------------------------------


def test_upload_over_the_configured_ceiling_is_refused_with_413(package_client, service, monkeypatch):
    monkeypatch.setattr(service, "archive_max_bytes", 8, raising=False)

    response = package_client.post(
        "/api/v1/lab-packages/inspect",
        files={"file": ("big.astinuslab", b"x" * 4096, "application/octet-stream")},
    )

    assert response.status_code == 413
    assert "too large" in response.json()["detail"]
    assert not list((service.package_root).glob(".astinuslab-upload-*"))


def test_upload_of_bytes_the_reader_rejects_is_refused_with_400(package_client, service):
    response = package_client.post(
        "/api/v1/lab-packages/inspect",
        files={"file": ("junk.astinuslab", b"not a zip archive at all", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert not list((service.package_root).glob(".astinuslab-upload-*"))


def test_upload_staging_failure_is_reported_and_cleans_up(package_client, service, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("storage disappeared mid-upload")

    monkeypatch.setattr(service, "finalize_staged_upload", refuse)

    response = package_client.post(
        "/api/v1/lab-packages/inspect",
        files={"file": ("api.astinuslab", _archive(), "application/octet-stream")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "could not stage package upload"
    assert not list((service.package_root).glob(".astinuslab-upload-*"))


# --- commit --------------------------------------------------------------------


def test_commit_of_an_expired_inspection_is_refused_with_410(package_client, service, monkeypatch):
    def expired(*_args, **_kwargs):
        raise PackageInspectionExpiredError("staged inspection expired")

    monkeypatch.setattr(service, "commit_inspection", expired)

    response = package_client.post("/api/v1/lab-packages/imports/lpi-gone/commit")

    assert response.status_code == 410
    assert "expired" in response.json()["detail"]


def test_commit_of_a_conflicting_identity_is_refused_with_409(package_client, service, monkeypatch):
    def conflict(*_args, **_kwargs):
        raise PackageImportConflictError("package identity already retained")

    monkeypatch.setattr(service, "commit_inspection", conflict)

    response = package_client.post("/api/v1/lab-packages/imports/lpi-dup/commit")

    assert response.status_code == 409


def test_commit_of_an_invalid_inspection_is_refused_with_400(package_client, service, monkeypatch):
    def invalid(*_args, **_kwargs):
        raise PackageImportError("staged archive is not readable")

    monkeypatch.setattr(service, "commit_inspection", invalid)

    response = package_client.post("/api/v1/lab-packages/imports/lpi-bad/commit")

    assert response.status_code == 400


# --- locked export preconditions -----------------------------------------------


def test_locked_export_without_a_verification_is_refused(package_client, service):
    response = package_client.post("/api/v1/lab-packages/export", json=_export_body(reproducibility="locked"))

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "verification_required"


def test_locked_export_embedding_without_redistribution_confirmation_is_refused(package_client, service):
    response = package_client.post(
        "/api/v1/lab-packages/export",
        json=_export_body(reproducibility="locked", verification_id="lpv-1", embed_assets=True),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "redistribution_unconfirmed"


def test_locked_export_accepts_the_legacy_confirmation_field(package_client, service):
    """``redistribution_confirmation`` overrides the older flag, so this gets past
    the embedding gate and fails on the unknown verification instead."""

    response = package_client.post(
        "/api/v1/lab-packages/export",
        json=_export_body(
            reproducibility="locked",
            verification_id="lpv-unknown",
            embed_assets=True,
            redistribution_confirmation=True,
        ),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] != "redistribution_unconfirmed"


def test_locked_export_with_an_unknown_verification_is_refused(package_client, service):
    response = package_client.post(
        "/api/v1/lab-packages/export",
        json=_export_body(
            reproducibility="locked",
            verification_id="lpv-unknown",
            embed_assets=False,
        ),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"]


def test_locked_export_reads_the_verification_id_from_the_legacy_field(package_client, service):
    response = package_client.post(
        "/api/v1/lab-packages/export",
        json=_export_body(
            reproducibility="locked",
            clean_run_verification_id="lpv-unknown",
            embed_assets=False,
        ),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] != "verification_required"


def test_export_rejects_an_unknown_field(package_client, service):
    response = package_client.post("/api/v1/lab-packages/export", json=_export_body(unexpected="value"))

    assert response.status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"package_id": "Not A Valid Id"},
        {"version": "not-a-version"},
        {"reproducibility": "partial"},
        {"runbook": ""},
        {"title": ""},
    ],
)
def test_export_validates_its_request_body(package_client, service, overrides):
    response = package_client.post("/api/v1/lab-packages/export", json=_export_body(**overrides))

    assert response.status_code == 422


# --- record lookups ------------------------------------------------------------


def test_detail_of_an_unknown_package_is_404(package_client, service):
    assert package_client.get("/api/v1/lab-packages/lp-absent").status_code == 404


def test_download_of_an_unknown_package_is_404(package_client, service):
    assert package_client.get("/api/v1/lab-packages/lp-absent/download").status_code == 404


def test_preflight_of_an_unknown_package_is_404(package_client, service):
    assert package_client.post("/api/v1/lab-packages/lp-absent/preflight").status_code == 404


def test_delete_of_an_unknown_package_is_404(package_client, service):
    assert package_client.delete("/api/v1/lab-packages/lp-absent").status_code == 404


# --- verification routes -------------------------------------------------------


def test_status_of_an_unknown_verification_is_404(package_client, service):
    response = package_client.get("/api/v1/lab-packages/verifications/lpv-absent")

    assert response.status_code == 404
    assert response.json()["detail"]["code"]


def test_cancelling_an_unknown_verification_is_404(package_client, service):
    response = package_client.post("/api/v1/lab-packages/verifications/lpv-absent/cancel")

    assert response.status_code == 404
    assert response.json()["detail"]["code"]


def test_a_draft_requires_a_valid_definition(package_client, service):
    response = package_client.post(
        "/api/v1/lab-packages/drafts", json={"definition": {"package_id": "x"}, "runbook": "# R\n"}
    )

    assert response.status_code == 422


def test_a_draft_is_saved_and_returns_its_digest(package_client, service):
    response = package_client.post(
        "/api/v1/lab-packages/drafts",
        json={"definition": _definition().model_dump(mode="json"), "runbook": "# Refusal runbook\n"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"].startswith("ld-")
    assert len(body["digest"]) == 64


def test_saving_the_same_draft_twice_is_idempotent(package_client, service):
    payload = {"definition": _definition().model_dump(mode="json"), "runbook": "# Refusal runbook\n"}

    first = package_client.post("/api/v1/lab-packages/drafts", json=payload)
    second = package_client.post("/api/v1/lab-packages/drafts", json=payload)

    assert first.json()["digest"] == second.json()["digest"]
    assert first.json()["id"] == second.json()["id"]


def test_a_draft_rejects_an_unknown_field(package_client, service):
    response = package_client.post(
        "/api/v1/lab-packages/drafts",
        json={
            "definition": _definition().model_dump(mode="json"),
            "runbook": "# R\n",
            "unexpected": "value",
        },
    )

    assert response.status_code == 422


# --- instance operation routes -------------------------------------------------


def test_status_of_an_unknown_instance_is_404(package_client, service):
    assert package_client.get("/api/v1/lab-packages/operations/lpi-absent").status_code == 404


def test_requesting_an_action_on_an_unknown_instance_is_404(package_client, service):
    response = package_client.post("/api/v1/lab-packages/operations/lpi-absent/reset")

    assert response.status_code == 404
    assert response.json()["detail"] == "package operation not found"


# --- prepare routes ------------------------------------------------------------


def test_prepare_status_for_an_unknown_attempt_is_404(package_client, service):
    assert package_client.get("/api/v1/lab-packages/prepare/lpa-absent").status_code == 404


def test_cancelling_an_unknown_prepare_is_404(package_client, service):
    assert package_client.post("/api/v1/lab-packages/prepare/lpa-absent/cancel").status_code == 404


def test_preparing_an_unknown_package_is_404(package_client, service):
    response = package_client.post("/api/v1/lab-packages/lp-absent/prepare", json={"idempotency_key": "k1"})

    assert response.status_code == 404


def test_launching_an_unknown_package_is_404(package_client, service):
    assert package_client.post("/api/v1/lab-packages/lp-absent/launch", json={}).status_code == 404


def test_listing_packages_is_empty_before_any_import(package_client, service):
    response = package_client.get("/api/v1/lab-packages")

    assert response.status_code == 200
    assert response.json() == []


def test_garbage_collection_runs_on_an_empty_store(package_client, service):
    response = package_client.post("/api/v1/lab-packages/gc")

    assert response.status_code == 200
    assert response.json()["removed_content"] == 0


# --- stored-state corruption ---------------------------------------------------


def _import_one(package_client, service) -> dict:
    inspected = package_client.post(
        "/api/v1/lab-packages/inspect",
        files={"file": ("refusal.astinuslab", _archive(), "application/octet-stream")},
    )
    assert inspected.status_code == 200, inspected.text
    committed = package_client.post(f"/api/v1/lab-packages/imports/{inspected.json()['inspection_id']}/commit")
    assert committed.status_code == 201, committed.text
    return committed.json()["package"]


def _corrupt(session, record_id: str, **columns) -> None:
    """Write past the ORM immutability guard.

    Package rows cannot be mutated through the ORM, which is the point of the
    guard. These branches defend against a row that was damaged underneath the
    application — storage corruption, a hand-edited database — so the test has
    to damage it the same way, at the SQL layer.
    """
    from sqlalchemy import update

    from astinuslab.db.models import LabPackage

    session.execute(update(LabPackage.__table__).where(LabPackage.id == record_id).values(**columns))  # type: ignore[arg-type]
    session.commit()
    session.expire_all()


def test_unreadable_stored_capabilities_are_reported_as_a_server_fault(
    package_client, service, in_memory_session_scope
):
    record = _import_one(package_client, service)
    with in_memory_session_scope() as session:
        _corrupt(session, record["id"], required_capabilities_json="{not json")

    assert package_client.get(f"/api/v1/lab-packages/{record['id']}").status_code == 500


def test_stored_capabilities_of_the_wrong_shape_are_reported_as_a_server_fault(
    package_client, service, in_memory_session_scope
):
    record = _import_one(package_client, service)
    with in_memory_session_scope() as session:
        _corrupt(session, record["id"], required_capabilities_json='{"a": 1}')

    assert package_client.get(f"/api/v1/lab-packages/{record['id']}").status_code == 500


def test_an_unreadable_stored_definition_fails_preflight_as_a_server_fault(
    package_client, service, in_memory_session_scope
):
    record = _import_one(package_client, service)
    with in_memory_session_scope() as session:
        _corrupt(session, record["id"], definition_json="{not json")

    assert package_client.post(f"/api/v1/lab-packages/{record['id']}/preflight").status_code == 500


# --- download integrity --------------------------------------------------------


def test_downloading_a_vanished_archive_is_410(package_client, service, in_memory_session_scope):
    from astinuslab.db.models import LabPackage

    record = _import_one(package_client, service)
    with in_memory_session_scope() as session:
        Path(session.get(LabPackage, record["id"]).archive_path).unlink()

    assert package_client.get(f"/api/v1/lab-packages/{record['id']}/download").status_code == 410


def test_downloading_an_archive_of_the_wrong_size_is_410(package_client, service, in_memory_session_scope):
    from astinuslab.db.models import LabPackage

    record = _import_one(package_client, service)
    with in_memory_session_scope() as session:
        Path(session.get(LabPackage, record["id"]).archive_path).write_bytes(b"short")

    assert package_client.get(f"/api/v1/lab-packages/{record['id']}/download").status_code == 410


def test_downloading_an_archive_replaced_by_a_symlink_is_410(
    package_client, service, in_memory_session_scope, tmp_path: Path
):
    from astinuslab.db.models import LabPackage

    record = _import_one(package_client, service)
    with in_memory_session_scope() as session:
        archive = Path(session.get(LabPackage, record["id"]).archive_path)
    elsewhere = tmp_path / "elsewhere.astinuslab"
    elsewhere.write_bytes(_archive())
    archive.unlink()
    archive.symlink_to(elsewhere)

    assert package_client.get(f"/api/v1/lab-packages/{record['id']}/download").status_code == 410


def test_downloading_an_intact_archive_returns_its_bytes(package_client, service):
    record = _import_one(package_client, service)

    response = package_client.get(f"/api/v1/lab-packages/{record['id']}/download")

    assert response.status_code == 200
    assert response.content == _archive()


# --- the one-shot import route -------------------------------------------------


def test_the_import_route_stages_and_commits_in_one_call(package_client, service):
    response = package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("refusal.astinuslab", _archive(), "application/octet-stream")},
    )

    assert response.status_code == 201, response.text
    assert response.json()["package"]["package_id"] == "refusal-lab"


def test_the_import_route_refuses_bytes_the_reader_rejects(package_client, service):
    response = package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("junk.astinuslab", b"not an archive", "application/octet-stream")},
    )

    assert response.status_code == 400


def test_importing_the_same_identity_twice_is_a_conflict(package_client, service, monkeypatch):
    package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("refusal.astinuslab", _archive(), "application/octet-stream")},
    )

    def conflict(*_args, **_kwargs):
        raise PackageImportConflictError("package identity already retained")

    monkeypatch.setattr(service, "commit_inspection", conflict)

    response = package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("refusal.astinuslab", _archive(), "application/octet-stream")},
    )

    assert response.status_code == 409


def test_the_import_route_reports_a_commit_failure_as_a_client_error(package_client, service, monkeypatch):
    def invalid(*_args, **_kwargs):
        raise PackageImportError("staged archive is not readable")

    monkeypatch.setattr(service, "commit_inspection", invalid)

    response = package_client.post(
        "/api/v1/lab-packages/import",
        files={"file": ("refusal.astinuslab", _archive(), "application/octet-stream")},
    )

    assert response.status_code == 400


# --- post-commit dispatch ------------------------------------------------------


def test_a_committed_launch_is_submitted_to_the_worker(monkeypatch):
    from astinuslab.lab_packages import api as package_api

    submitted: list[str] = []

    class _Task:
        def delay(self, instance_id):
            submitted.append(instance_id)

    monkeypatch.setattr("astinuslab.jobs.package_instance_coordinator.coordinate_instance", _Task(), raising=False)

    package_api.dispatch_instance_after_commit("lpi-1")

    assert submitted == ["lpi-1"]


def test_a_committed_action_is_submitted_to_the_worker(monkeypatch):
    from astinuslab.lab_packages import api as package_api

    submitted: list[tuple[str, str]] = []

    class _Task:
        def delay(self, instance_id, action):
            submitted.append((instance_id, action))

    monkeypatch.setattr(
        "astinuslab.jobs.package_instance_coordinator.coordinate_instance_action", _Task(), raising=False
    )

    package_api.dispatch_instance_action_after_commit("lpi-1", "teardown")

    assert submitted == [("lpi-1", "teardown")]


def test_a_broker_failure_does_not_break_the_committed_request(monkeypatch):
    """The durable outbox row is the recovery path.

    ``get_session`` runs registered dispatchers only after the transaction has
    committed, so a broker that is down must be logged and swallowed rather
    than failing a request whose work is already durable.
    """

    from astinuslab.lab_packages import api as package_api

    dispatched: list[str] = []
    generator = package_api.get_session()
    session = next(generator)
    session.info.setdefault("package_post_commit", []).extend(
        [
            lambda: (_ for _ in ()).throw(RuntimeError("broker down")),
            lambda: dispatched.append("second"),
        ]
    )

    with pytest.raises(StopIteration):
        next(generator)

    # The first dispatcher failed; the second still ran.
    assert dispatched == ["second"]


def test_a_rolled_back_request_dispatches_nothing():
    from astinuslab.lab_packages import api as package_api

    dispatched: list[str] = []
    generator = package_api.get_session()
    session = next(generator)
    session.info.setdefault("package_post_commit", []).append(lambda: dispatched.append("sent"))

    with pytest.raises(RuntimeError, match="request failed"):
        generator.throw(RuntimeError("request failed"))

    assert dispatched == []
