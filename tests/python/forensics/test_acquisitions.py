"""Tests for AcquisitionRepository and acquisition-aware JobRepository methods."""

from __future__ import annotations

import pytest
from sqlalchemy import event

from astinuslab.api.schemas import JobCreate
from astinuslab.db.models import Job
from astinuslab.db.repositories import (
    AcquisitionRepository,
    ArtifactRepository,
    CaseRepository,
    JobRepository,
    ReportRepository,
)
from astinuslab.forensics.case_services import list_acquisitions_service


def _create_artifact_job_owner(session):
    session.add(
        Job(id="job-api", target_id="fixture", scenario_id="fixture", execution_mode="immediate", status="completed")
    )
    session.flush()


@pytest.fixture()
def acq_repo(in_memory_session_scope):
    with in_memory_session_scope() as session:
        yield AcquisitionRepository(session), session


class TestCaseRepository:
    def test_create_case(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = CaseRepository(session)
            case = repo.create_case("incident-a")
            assert case.id.startswith("case-")
            assert case.name == "incident-a"
            assert case.status == "open"
            assert case.closed_at is None

    def test_list_cases_returns_newest_first(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = CaseRepository(session)
            repo.create_case("first")
            repo.create_case("second")
            result = repo.list_cases()
            assert result[0].name == "second"
            assert result[1].name == "first"


class TestArtifactRepository:
    def test_create_artifact_linked_to_job_acquisition_and_case(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            case = CaseRepository(session).create_case("incident-a")
            acq = AcquisitionRepository(session).create_acquisition("capture-a", case_id=case.id)
            job = JobRepository(session).create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            artifact = ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path="/tmp/memory.dump",
                filename="memory.dump",
                sha256="a" * 64,
                size_bytes=10,
                manifest_path="/tmp/memory.dump.manifest.json",
                sha256_sidecar_path="/tmp/memory.dump.sha256",
                case_id=case.id,
                job_id=job.id,
                acquisition_id=acq.id,
            )

            assert artifact.id.startswith("art-")
            assert artifact.kind == "memory"
            assert artifact.producer == "job_capture"
            assert artifact.case_id == case.id
            assert artifact.job_id == job.id
            assert artifact.acquisition_id == acq.id
            assert artifact.manifest_path == "/tmp/memory.dump.manifest.json"
            assert artifact.sha256_sidecar_path == "/tmp/memory.dump.sha256"

    def test_list_artifacts_filters_by_acquisition(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            repo = ArtifactRepository(session)
            acq_repo = AcquisitionRepository(session)
            acq_a = acq_repo.create_acquisition("a")
            acq_b = acq_repo.create_acquisition("b")
            artifact_a = repo.create_artifact(
                kind="memory",
                producer="playground_capture",
                path="/tmp/a.dump",
                acquisition_id=acq_a.id,
            )
            repo.create_artifact(
                kind="disk",
                producer="playground_capture",
                path="/tmp/b.qcow2",
                acquisition_id=acq_b.id,
            )

            result = repo.list_artifacts(acquisition_id=acq_a.id)
            assert [artifact.id for artifact in result] == [artifact_a.id]


class TestReportRepository:
    def test_create_report_links_input_and_output_artifacts(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            case = CaseRepository(session).create_case("incident-a")
            job = JobRepository(session).create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            artifact_repo = ArtifactRepository(session)
            input_artifact = artifact_repo.create_artifact(
                kind="memory",
                producer="job_capture",
                path="/tmp/memory.dump",
                sha256="a" * 64,
                case_id=case.id,
                job_id=job.id,
            )
            output_artifact = artifact_repo.create_artifact(
                kind="report",
                producer="vol3_analysis",
                path="/tmp/analysis-summary.json",
                sha256="b" * 64,
                case_id=case.id,
                job_id=job.id,
            )

            report = ReportRepository(session).create_report(
                report_type="volatility3",
                status="completed",
                path="/tmp/analysis-summary.json",
                title="Volatility 3 analysis",
                summary="2 plugins completed",
                case_id=case.id,
                job_id=job.id,
                input_artifact_id=input_artifact.id,
                output_artifact_id=output_artifact.id,
                metadata_json='{"plugins": 2}',
            )

            assert report.id.startswith("rep-")
            assert report.report_type == "volatility3"
            assert report.status == "completed"
            assert report.case_id == case.id
            assert report.job_id == job.id
            assert report.input_artifact_id == input_artifact.id
            assert report.output_artifact_id == output_artifact.id
            assert report.path == "/tmp/analysis-summary.json"

    def test_list_reports_filters_by_job_and_artifacts(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            artifact_repo = ArtifactRepository(session)
            input_a = artifact_repo.create_artifact(kind="memory", producer="job_capture", path="/tmp/a.dump")
            output_a = artifact_repo.create_artifact(kind="report", producer="vol3_analysis", path="/tmp/a.json")
            input_b = artifact_repo.create_artifact(kind="memory", producer="job_capture", path="/tmp/b.dump")
            output_b = artifact_repo.create_artifact(kind="report", producer="vol3_analysis", path="/tmp/b.json")
            repo = ReportRepository(session)
            report_a = repo.create_report(
                report_type="volatility3",
                status="completed",
                path="/tmp/a.json",
                job_id="job-a",
                input_artifact_id=input_a.id,
                output_artifact_id=output_a.id,
            )
            repo.create_report(
                report_type="volatility3",
                status="completed",
                path="/tmp/b.json",
                job_id="job-b",
                input_artifact_id=input_b.id,
                output_artifact_id=output_b.id,
            )

            assert [r.id for r in repo.list_reports(job_id="job-a")] == [report_a.id]
            assert [r.id for r in repo.list_reports(input_artifact_id=input_a.id)] == [report_a.id]
            assert [r.id for r in repo.list_reports(output_artifact_id=output_a.id)] == [report_a.id]


class TestAcquisitionRepository:
    def test_create_acquisition(self, acq_repo):
        repo, _ = acq_repo
        acq = repo.create_acquisition("pentest-q1")
        assert acq.id.startswith("acq-")
        assert acq.name == "pentest-q1"
        assert acq.status == "live"
        assert acq.sealed_at is None

    def test_ids_are_unique(self, acq_repo):
        repo, _ = acq_repo
        a1 = repo.create_acquisition("a")
        a2 = repo.create_acquisition("b")
        assert a1.id != a2.id

    def test_list_acquisitions_returns_newest_first(self, acq_repo):
        repo, _ = acq_repo
        repo.create_acquisition("first")
        repo.create_acquisition("second")
        result = repo.list_acquisitions()
        assert result[0].name == "second"
        assert result[1].name == "first"

    def test_list_acquisitions_counts_jobs_in_one_query(self, in_memory_session_scope):
        from datetime import UTC, datetime

        from astinuslab.db.models import Acquisition, Job

        created_at = datetime(2026, 1, 2, tzinfo=UTC)
        with in_memory_session_scope() as session:
            session.add_all(
                [
                    Acquisition(id=f"acq-{index:02d}", name=str(index), status="live", created_at=created_at)
                    for index in range(12)
                ]
            )
            session.add_all(
                [
                    Job(
                        id="job-live",
                        target_id="ubuntu-22-04-default",
                        scenario_id="immediate-capture",
                        execution_mode="immediate",
                        capture_type="memory",
                        status="queued",
                        acquisition_id="acq-11",
                        created_at=created_at,
                    ),
                    Job(
                        id="job-finished",
                        target_id="ubuntu-22-04-default",
                        scenario_id="immediate-capture",
                        execution_mode="immediate",
                        capture_type="memory",
                        status="completed",
                        acquisition_id="acq-11",
                        created_at=created_at,
                    ),
                ]
            )
            session.commit()
            session.expire_all()
            statements: list[str] = []

            def count_selects(conn, cursor, statement, parameters, context, executemany):
                if statement.lstrip().upper().startswith("SELECT"):
                    statements.append(statement)

            event.listen(session.bind, "before_cursor_execute", count_selects)
            try:
                results = list_acquisitions_service(session)
            finally:
                event.remove(session.bind, "before_cursor_execute", count_selects)

        assert len(statements) == 1
        assert [result.id for result in results[:2]] == ["acq-11", "acq-10"]
        assert results[0].job_count == 2
        assert results[0].live_job_count == 1
        assert results[1].job_count == 0
        assert results[1].live_job_count == 0

    def test_get_acquisition_returns_none_for_unknown(self, acq_repo):
        repo, _ = acq_repo
        assert repo.get_acquisition("acq-doesnotexist") is None

    def test_get_acquisition_returns_existing(self, acq_repo):
        repo, _ = acq_repo
        acq = repo.create_acquisition("my-lab")
        fetched = repo.get_acquisition(acq.id)
        assert fetched is not None
        assert fetched.name == "my-lab"

    def test_update_status_to_sealed_sets_sealed_at(self, acq_repo):
        repo, _ = acq_repo
        acq = repo.create_acquisition("lab")
        updated = repo.update_acquisition_status(acq.id, "sealed")
        assert updated.status == "sealed"
        assert updated.sealed_at is not None

    def test_update_status_raises_for_unknown(self, acq_repo):
        repo, _ = acq_repo
        with pytest.raises(ValueError, match="not found"):
            repo.update_acquisition_status("acq-ghost", "sealed")


class TestJobRepositoryAcquisitionMethods:
    def test_list_captures_excludes_manual_trigger(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            job_repo = JobRepository(session)
            imm = job_repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            mt = job_repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                )
            )
            result = job_repo.list_captures()
            ids = [j.id for j in result]
            assert imm.id in ids
            assert mt.id not in ids

    def test_list_captures_includes_post_script(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            job_repo = JobRepository(session)
            ps = job_repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="workload-sample-post-script",
                    execution_mode="post_script",
                )
            )
            result = job_repo.list_captures()
            assert ps.id in [j.id for j in result]

    def test_list_jobs_by_acquisition(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            job_repo = JobRepository(session)
            acq_repo = AcquisitionRepository(session)
            acq = acq_repo.create_acquisition("lab")
            j1 = job_repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                )
            )
            j2 = job_repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                )
            )
            j1.acquisition_id = acq.id
            j2.acquisition_id = acq.id
            session.flush()
            result = job_repo.list_jobs_by_acquisition(acq.id)
            assert {j.id for j in result} == {j1.id, j2.id}

    def test_list_jobs_by_acquisition_excludes_other_acquisitions(self, in_memory_session_scope):
        with in_memory_session_scope() as session:
            job_repo = JobRepository(session)
            acq_repo = AcquisitionRepository(session)
            acq_a = acq_repo.create_acquisition("a")
            acq_b = acq_repo.create_acquisition("b")
            ja = job_repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                )
            )
            jb = job_repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="lab-session",
                    execution_mode="manual_trigger",
                )
            )
            ja.acquisition_id = acq_a.id
            jb.acquisition_id = acq_b.id
            session.flush()
            assert [j.id for j in job_repo.list_jobs_by_acquisition(acq_a.id)] == [ja.id]


class TestAcquisitionAPI:
    def test_create_acquisition(self, client):
        r = client.post("/api/v1/acquisitions", json={"name": "recon-q2"})
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "recon-q2"
        assert data["status"] == "live"
        assert data["id"].startswith("acq-")
        assert data["job_count"] == 0

    def test_list_acquisitions_empty(self, client):
        r = client.get("/api/v1/acquisitions")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_acquisitions_returns_created(self, client):
        client.post("/api/v1/acquisitions", json={"name": "lab-a"})
        r = client.get("/api/v1/acquisitions")
        assert len(r.json()) == 1
        assert r.json()[0]["name"] == "lab-a"

    def test_get_acquisition(self, client):
        created = client.post("/api/v1/acquisitions", json={"name": "my-lab"}).json()
        r = client.get(f"/api/v1/acquisitions/{created['id']}")
        assert r.status_code == 200
        assert r.json()["name"] == "my-lab"

    def test_get_acquisition_404(self, client):
        r = client.get("/api/v1/acquisitions/acq-doesnotexist")
        assert r.status_code == 404

    def test_create_acquisition_rejects_empty_name(self, client):
        r = client.post("/api/v1/acquisitions", json={"name": ""})
        assert r.status_code == 422

    def test_update_acquisition_status_to_sealed(self, client):
        created = client.post("/api/v1/acquisitions", json={"name": "lab"}).json()
        r = client.patch(
            f"/api/v1/acquisitions/{created['id']}/status",
            json={"status": "sealed"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "sealed"
        assert r.json()["sealed_at"] is not None

    def test_update_acquisition_status_404(self, client):
        r = client.patch(
            "/api/v1/acquisitions/acq-ghost/status",
            json={"status": "sealed"},
        )
        assert r.status_code == 404

    def test_update_acquisition_status_rejects_invalid(self, client):
        created = client.post("/api/v1/acquisitions", json={"name": "lab"}).json()
        r = client.patch(
            f"/api/v1/acquisitions/{created['id']}/status",
            json={"status": "nonsense"},
        )
        assert r.status_code == 422


class TestCaseAPI:
    def test_create_case(self, client):
        r = client.post("/api/v1/cases", json={"name": "incident-a"})
        assert r.status_code == 201
        data = r.json()
        assert data["id"].startswith("case-")
        assert data["name"] == "incident-a"
        assert data["status"] == "open"
        assert data["closed_at"] is None

    def test_list_cases_returns_created(self, client):
        client.post("/api/v1/cases", json={"name": "incident-a"})
        r = client.get("/api/v1/cases")
        assert r.status_code == 200
        assert r.json()[0]["name"] == "incident-a"

    def test_update_case_status_to_closed(self, client):
        created = client.post("/api/v1/cases", json={"name": "incident-a"}).json()
        r = client.patch(f"/api/v1/cases/{created['id']}/status", json={"status": "closed"})
        assert r.status_code == 200
        assert r.json()["status"] == "closed"
        assert r.json()["closed_at"] is not None


class TestArtifactsAPI:
    def test_list_artifacts_filters_by_acquisition(self, client, in_memory_session_scope):
        with in_memory_session_scope() as session:
            artifact = ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path="/tmp/memory.dump",
                filename="memory.dump",
                sha256="a" * 64,
                size_bytes=1,
                manifest_path="/tmp/memory.dump.manifest.json",
                sha256_sidecar_path="/tmp/memory.dump.sha256",
                acquisition_id="acq-api",
            )
            artifact_id = artifact.id

        r = client.get("/api/v1/artifacts", params={"acquisition_id": "acq-api"})
        assert r.status_code == 200
        data = r.json()
        assert [row["id"] for row in data] == [artifact_id]
        assert data[0]["kind"] == "memory"
        assert "manifest_path" not in data[0]
        assert "sha256_sidecar_path" not in data[0]

    def test_download_artifact_manifest_and_sha256_sidecar(self, client, in_memory_session_scope, tmp_path):
        import astinuslab.jobs.job_services as svc_mod

        storage_dir = svc_mod.settings.storage_dir
        dump_dir = storage_dir / "dumps" / f"job-api-{tmp_path.name}"
        dump_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = dump_dir / "memory.dump"
        manifest_path = dump_dir / "memory.dump.manifest.json"
        sidecar_path = dump_dir / "memory.dump.sha256"
        artifact_path.write_text("dump-bytes", encoding="utf-8")
        manifest_path.write_text('{"schema_version":"artifact-manifest/v1"}', encoding="utf-8")
        sidecar_path.write_text(f"{'a' * 64}  memory.dump\n", encoding="utf-8")

        with in_memory_session_scope() as session:
            _create_artifact_job_owner(session)
            artifact = ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path=str(artifact_path),
                filename="memory.dump",
                sha256="a" * 64,
                manifest_path=str(manifest_path),
                sha256_sidecar_path=str(sidecar_path),
                job_id="job-api",
            )
            artifact_id = artifact.id

        artifact_response = client.get(f"/api/v1/artifacts/{artifact_id}/download")
        manifest_response = client.get(f"/api/v1/artifacts/{artifact_id}/manifest")
        sidecar_response = client.get(f"/api/v1/artifacts/{artifact_id}/sha256")

        assert artifact_response.status_code == 200
        assert artifact_response.text == "dump-bytes"
        assert artifact_response.headers["x-astinuslab-sha256"] == "a" * 64
        assert manifest_response.status_code == 200
        assert manifest_response.json()["schema_version"] == "artifact-manifest/v1"
        assert sidecar_response.status_code == 200
        assert sidecar_response.text == f"{'a' * 64}  memory.dump\n"

    def test_download_artifact_returns_404_when_file_missing(self, client, in_memory_session_scope):
        import astinuslab.jobs.job_services as svc_mod

        missing_path = svc_mod.settings.storage_dir / "dumps" / "missing-memory.dump"
        if missing_path.exists():
            missing_path.unlink()
        with in_memory_session_scope() as session:
            _create_artifact_job_owner(session)
            artifact = ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path=str(missing_path),
                filename="memory.dump",
                job_id="job-api",
            )
            artifact_id = artifact.id

        response = client.get(f"/api/v1/artifacts/{artifact_id}/download")

        assert response.status_code == 404

    def test_download_artifact_manifest_returns_404_when_manifest_missing(self, client, in_memory_session_scope):
        import astinuslab.jobs.job_services as svc_mod

        artifact_path = svc_mod.settings.storage_dir / "dumps" / "memory-without-manifest.dump"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("dump-bytes", encoding="utf-8")
        with in_memory_session_scope() as session:
            _create_artifact_job_owner(session)
            artifact = ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path=str(artifact_path),
                filename="memory-without-manifest.dump",
                job_id="job-api",
            )
            artifact_id = artifact.id

        response = client.get(f"/api/v1/artifacts/{artifact_id}/manifest")

        assert response.status_code == 404

    def test_job_detail_renders_artifact_report_manifest_and_sidecar_links(
        self, client, in_memory_session_scope, tmp_path
    ):
        dump_dir = tmp_path / "storage" / "dumps" / "job-ui"
        dump_dir.mkdir(parents=True)
        memory_path = dump_dir / "memory.dump"
        memory_manifest_path = dump_dir / "memory.dump.manifest.json"
        memory_sidecar_path = dump_dir / "memory.dump.sha256"
        report_path = dump_dir / "vol3" / "analysis-summary.json"
        report_manifest_path = dump_dir / "vol3" / "analysis-summary.json.manifest.json"
        report_sidecar_path = dump_dir / "vol3" / "analysis-summary.json.sha256"
        report_path.parent.mkdir(parents=True)

        with in_memory_session_scope() as session:
            job = JobRepository(session).create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            artifact_repo = ArtifactRepository(session)
            input_artifact = artifact_repo.create_artifact(
                kind="memory",
                producer="job_capture",
                path=str(memory_path),
                filename="memory.dump",
                sha256="a" * 64,
                manifest_path=str(memory_manifest_path),
                sha256_sidecar_path=str(memory_sidecar_path),
                job_id=job.id,
            )
            output_artifact = artifact_repo.create_artifact(
                kind="report",
                producer="vol3_analysis",
                path=str(report_path),
                filename="analysis-summary.json",
                sha256="b" * 64,
                manifest_path=str(report_manifest_path),
                sha256_sidecar_path=str(report_sidecar_path),
                job_id=job.id,
            )
            ReportRepository(session).create_report(
                report_type="volatility3",
                status="completed",
                path=str(report_path),
                title="Volatility 3 analysis",
                summary="2 plugins completed",
                job_id=job.id,
                input_artifact_id=input_artifact.id,
                output_artifact_id=output_artifact.id,
            )
            job_id = job.id
            input_id = input_artifact.id
            output_id = output_artifact.id

        response = client.get(f"/jobs/{job_id}")

        assert response.status_code == 200
        assert "Evidence artifacts" in response.text
        assert "memory.dump" in response.text
        assert "analysis-summary.json" in response.text
        assert f"/api/v1/artifacts/{input_id}/download" in response.text
        assert f"/api/v1/artifacts/{input_id}/manifest" in response.text
        assert f"/api/v1/artifacts/{input_id}/sha256" in response.text
        assert f"/api/v1/artifacts/{output_id}/download" in response.text
        assert f"/api/v1/artifacts/{output_id}/manifest" in response.text
        assert f"/api/v1/artifacts/{output_id}/sha256" in response.text
        assert "Volatility 3 analysis" in response.text


class TestReportsAPI:
    def test_list_reports_filters_by_job(self, client, in_memory_session_scope):
        with in_memory_session_scope() as session:
            _create_artifact_job_owner(session)
            artifact = ArtifactRepository(session).create_artifact(
                kind="report",
                producer="vol3_analysis",
                path="/tmp/analysis-summary.json",
                filename="analysis-summary.json",
                sha256="b" * 64,
                job_id="job-api",
            )
            report = ReportRepository(session).create_report(
                report_type="volatility3",
                status="completed",
                path="/tmp/analysis-summary.json",
                title="Volatility 3 analysis",
                job_id="job-api",
                output_artifact_id=artifact.id,
            )
            report_id = report.id
            artifact_id = artifact.id

        r = client.get("/api/v1/reports", params={"job_id": "job-api"})
        assert r.status_code == 200
        data = r.json()
        assert [row["id"] for row in data] == [report_id]
        assert data[0]["report_type"] == "volatility3"
        assert data[0]["output_artifact_id"] == artifact_id

    def test_get_report(self, client, in_memory_session_scope):
        with in_memory_session_scope() as session:
            report = ReportRepository(session).create_report(
                report_type="volatility3",
                status="completed",
                path="/tmp/analysis-summary.json",
            )
            report_id = report.id

        r = client.get(f"/api/v1/reports/{report_id}")
        assert r.status_code == 200
        assert r.json()["id"] == report_id

    def test_get_report_404(self, client):
        r = client.get("/api/v1/reports/rep-doesnotexist")
        assert r.status_code == 404


class TestAcquisitionsUIHidden:
    def test_acquisitions_page_is_not_rendered(self, client):
        r = client.get("/acquisitions")
        assert r.status_code == 404

    def test_create_acquisition_form_route_is_not_rendered(self, client):
        r = client.post(
            "/acquisitions/new",
            data={
                "name": "my-lab",
                "target_id": "ubuntu-22-04-default",
                "scenario_id": "lab-session",
            },
            follow_redirects=False,
        )
        assert r.status_code == 404

    def test_acquisition_detail_page_is_not_rendered(self, client):
        acq_id = client.post("/api/v1/acquisitions", json={"name": "lab-detail"}).json()["id"]
        r = client.get(f"/acquisitions/{acq_id}")
        assert r.status_code == 404


class TestAcquireNeverBuildsSymbols:
    """ISF/symbol generation must never be coupled to a memory capture — it would
    SSH the live guest and contaminate the dump. ISF is built only as a separate job."""

    def _make_waiting_job(self, in_memory_session_scope):
        """Helper: create a job and put it in waiting_for_capture state."""

        with in_memory_session_scope() as session:
            repo = JobRepository(session)
            job = repo.create_job(
                JobCreate(
                    target_id="ubuntu-22-04-default",
                    scenario_id="immediate-capture",
                    execution_mode="immediate",
                )
            )
            repo.update_job(
                job.id,
                status="waiting_for_capture",
                current_stage="waiting_for_capture",
            )
            return job.id

    def test_acquire_payload_never_requests_symbol_build(self, in_memory_session_scope):
        import json

        from astinuslab.forensics.services import enqueue_acquire_memory_dump_service

        job_id = self._make_waiting_job(in_memory_session_scope)

        with in_memory_session_scope() as session:
            enqueue_acquire_memory_dump_service(session, job_id)
            repo = JobRepository(session)
            actions = repo.list_actions(job_id)
            dump_action = next(a for a in actions if a.action_type == "acquire_memory_dump")
            payload = json.loads(dump_action.payload or "{}")

        assert "build_vol3_symbols" not in payload

    def test_enqueue_acquire_rejects_symbol_build_kwarg(self, in_memory_session_scope):
        from astinuslab.forensics.services import enqueue_acquire_memory_dump_service

        job_id = self._make_waiting_job(in_memory_session_scope)

        with in_memory_session_scope() as session:
            with pytest.raises(TypeError):
                enqueue_acquire_memory_dump_service(  # type: ignore[call-arg]
                    session, job_id, build_vol3_symbols=True
                )
