"""Task 17 — the publication journal, which makes evidence publication recoverable.

Today a capture writes bytes, a manifest, a sidecar, and a database row in four
separate steps with no durable record tying them together. A crash between any
two leaves a state nothing can reconcile: an Acquisition stuck running, an
Artifact row pointing at bytes that were never renamed into place, or bytes on
disk no row knows about. The journal records the intent before the work starts,
so reconciliation has something to read.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from astinuslab.db import models
from astinuslab.storage import publication
from astinuslab.storage.artifacts import build_artifact_manifest

LIVE_STATES = ("reserved", "capturing", "staged", "db_prepared", "published", "completed")


def _attempt(session, **overrides):
    values = {
        "id": overrides.pop("id", "attempt-1"),
        "producer": "playground_capture",
        "kind": "memory_dump",
        "staging_dir": "/srv/storage/.publish/attempt-1",
        "final_path": "/srv/storage/dumps/job-1/memory.dump",
        "state": "reserved",
    }
    values.update(overrides)
    row = models.ArtifactPublicationAttempt(**values)
    session.add(row)
    session.flush()
    return row


def test_journal_records_the_publication_intent(in_memory_session_scope):
    with in_memory_session_scope() as session:
        row = _attempt(session)

        assert row.state == "reserved"
        assert row.created_at is not None
        assert row.completed_at is None
        assert row.artifact_id is None and row.acquisition_id is None


def test_state_is_constrained_to_the_documented_set(in_memory_session_scope):
    with in_memory_session_scope() as session:
        with pytest.raises(IntegrityError):
            _attempt(session, id="attempt-bogus", state="halfway")
        # The constraint fired mid-flush, so the transaction has to be unwound
        # before the fixture's commit runs.
        session.rollback()


@pytest.mark.parametrize("state", LIVE_STATES)
def test_one_live_attempt_per_final_path(in_memory_session_scope, state):
    """Two producers must not both believe they own the same destination."""
    with in_memory_session_scope() as session:
        _attempt(session, id="first", state=state)

        with pytest.raises(IntegrityError):
            _attempt(session, id="second", state=state)
        session.rollback()


@pytest.mark.parametrize("state", ("failed", "cleanup_incomplete"))
def test_a_dead_attempt_does_not_block_a_retry(in_memory_session_scope, state):
    """A failed attempt must not poison its destination forever."""
    with in_memory_session_scope() as session:
        _attempt(session, id="first", state=state)

        retry = _attempt(session, id="second", state="reserved")

        assert retry.id == "second"


def test_reserve_returns_a_reserved_attempt(in_memory_session_scope):
    with in_memory_session_scope() as session:
        attempt = publication.reserve(
            session,
            producer="job_capture",
            kind="disk_image",
            final_path="/srv/storage/dumps/job-2/disk.img.gz",
            staging_dir="/srv/storage/.publish/attempt-2",
            job_id="job-2",
        )

        assert attempt.state == "reserved"
        assert attempt.job_id == "job-2"
        assert attempt.id


def test_transition_advances_state_and_stamps_completion(in_memory_session_scope):
    with in_memory_session_scope() as session:
        attempt = publication.reserve(
            session,
            producer="job_capture",
            kind="disk_image",
            final_path="/srv/storage/dumps/job-3/disk.img.gz",
            staging_dir="/srv/storage/.publish/attempt-3",
        )
        before = attempt.updated_at

        publication.transition(session, attempt, "capturing")
        assert attempt.state == "capturing"
        assert attempt.completed_at is None
        assert attempt.updated_at >= before

        publication.transition(session, attempt, "completed")
        assert attempt.state == "completed"
        assert isinstance(attempt.completed_at, datetime)


def test_transition_refuses_an_unknown_state(in_memory_session_scope):
    with in_memory_session_scope() as session:
        attempt = publication.reserve(
            session,
            producer="job_capture",
            kind="disk_image",
            final_path="/srv/storage/dumps/job-4/disk.img.gz",
            staging_dir="/srv/storage/.publish/attempt-4",
        )

        with pytest.raises(ValueError):
            publication.transition(session, attempt, "halfway")


def test_failure_records_a_summary_operators_can_read(in_memory_session_scope):
    with in_memory_session_scope() as session:
        attempt = publication.reserve(
            session,
            producer="manual_upload",
            kind="memory_dump",
            final_path="/srv/storage/dumps/upload-1/memory.dump",
            staging_dir="/srv/storage/.publish/attempt-5",
        )

        publication.fail(session, attempt, "sha256 mismatch after staging")

        assert attempt.state == "failed"
        assert attempt.error_summary == "sha256 mismatch after staging"
        assert attempt.completed_at is not None


def test_open_attempts_are_what_reconciliation_reads(in_memory_session_scope):
    with in_memory_session_scope() as session:
        live = publication.reserve(
            session,
            producer="job_capture",
            kind="memory_dump",
            final_path="/srv/storage/dumps/job-9/memory.dump",
            staging_dir="/srv/storage/.publish/attempt-9",
        )
        done = publication.reserve(
            session,
            producer="job_capture",
            kind="memory_dump",
            final_path="/srv/storage/dumps/job-10/memory.dump",
            staging_dir="/srv/storage/.publish/attempt-10",
        )
        publication.transition(session, done, "completed")
        session.flush()

        open_ids = [row.id for row in publication.open_attempts(session)]

        assert live.id in open_ids
        assert done.id not in open_ids


def test_now_is_timezone_aware_utc():
    """A naive timestamp compared against an aware one raises at runtime."""
    assert publication._now().tzinfo is UTC


# ---------------------------------------------------------------------------
# Task 18 — attempt-owned staging and atomic triad creation
#
# `attempt()` takes a session *factory*, not a session: a capture runs for
# minutes, and a session held open across it would keep a SQLite write
# transaction open the whole time.
# ---------------------------------------------------------------------------


def _manifest(sha256: str, size_bytes: int) -> dict:
    return {
        "schema_version": "artifact-manifest/v1",
        "sha256": sha256,
        "size_bytes": size_bytes,
        "producer": "job_capture",
    }


@pytest.fixture()
def storage_root(tmp_path):
    root = tmp_path / "storage"
    root.mkdir(mode=0o750)
    (root / "dumps").mkdir(mode=0o750)
    return root


def _begin(scope, storage_root, name="memory.dump", **overrides):
    values = {
        "producer": "job_capture",
        "kind": "memory_dump",
        "final_path": storage_root / "dumps" / name,
        "storage_root": storage_root,
        "job_id": "job-1",
    }
    values.update(overrides)
    return publication.attempt(scope, **values)


def _publish(attempt, scope, payload=b"bytes"):
    """The full success path a producer follows."""
    with attempt.open_payload() as handle:
        handle.write(payload)
    attempt.stage(_manifest)
    published = attempt.publish()
    with scope() as session:
        attempt.complete(session)
    attempt.finish()
    return published


def test_staging_is_private_attempt_owned_and_under_the_storage_root(in_memory_session_scope, storage_root):
    """Never /tmp: every unit sets PrivateTmp=true, so a file staged in /tmp is
    invisible to the other services and to qemu, which runs outside the namespace."""
    with _begin(in_memory_session_scope, storage_root) as attempt:
        staging = attempt.staging_dir

        assert staging.parent == storage_root / ".publish"
        assert staging.is_relative_to(storage_root)
        assert attempt.id in staging.name
        assert stat.S_IMODE(staging.stat().st_mode) == 0o700
        attempt.abandon()


def test_payload_refuses_a_symlink_planted_at_its_path(in_memory_session_scope, storage_root):
    with pytest.raises(OSError):
        with _begin(in_memory_session_scope, storage_root) as attempt:
            (attempt.staging_dir / "payload").symlink_to("/etc/passwd")
            with attempt.open_payload():
                pass


def test_payload_cannot_be_opened_twice(in_memory_session_scope, storage_root):
    with pytest.raises(FileExistsError):
        with _begin(in_memory_session_scope, storage_root) as attempt:
            with attempt.open_payload() as handle:
                handle.write(b"first")
            with attempt.open_payload():
                pass


def test_capture_to_path_accepts_a_producer_written_file(in_memory_session_scope, storage_root):
    """libvirt writes a core dump itself; it cannot be handed an open descriptor."""
    with _begin(in_memory_session_scope, storage_root) as attempt:
        with attempt.capture_to_path() as target:
            target.write_bytes(b"dumped by the hypervisor")
        triad = attempt.stage(_manifest)

        assert triad.sha256 == hashlib.sha256(b"dumped by the hypervisor").hexdigest()
        attempt.abandon()


def test_capture_to_path_refuses_a_producer_that_wrote_nothing(in_memory_session_scope, storage_root):
    with pytest.raises(OSError, match="no payload"):
        with _begin(in_memory_session_scope, storage_root) as attempt:
            with attempt.capture_to_path():
                pass


def test_stage_records_the_hash_size_and_triad(in_memory_session_scope, storage_root):
    payload = b"memory image bytes"
    expected = hashlib.sha256(payload).hexdigest()
    with _begin(in_memory_session_scope, storage_root) as attempt:
        with attempt.open_payload() as handle:
            handle.write(payload)
        triad = attempt.stage(_manifest)

        assert triad.sha256 == expected
        assert triad.size_bytes == len(payload)
        assert attempt.record.state == "staged"
        assert json.loads(triad.manifest_path.read_text())["sha256"] == expected
        assert triad.sidecar_path.read_text().startswith(expected)
        attempt.abandon()


def test_stage_refuses_when_the_payload_inode_changes_under_it(in_memory_session_scope, storage_root, monkeypatch):
    """A swap between hashing and manifest writing would publish a manifest that
    describes bytes nobody has."""
    with pytest.raises(OSError):
        with _begin(in_memory_session_scope, storage_root) as attempt:
            with attempt.open_payload() as handle:
                handle.write(b"original")

            real_hash = publication._hash_payload

            def swap_then_hash(fd, path):
                result = real_hash(fd, path)
                path.unlink()
                path.write_bytes(b"substituted")
                return result

            monkeypatch.setattr(publication, "_hash_payload", swap_then_hash)
            attempt.stage(_manifest)


def test_publish_moves_the_whole_triad_into_place(in_memory_session_scope, storage_root):
    payload = b"disk image bytes"
    with _begin(in_memory_session_scope, storage_root) as attempt:
        published = _publish(attempt, in_memory_session_scope, payload)

    assert published.artifact_path.read_bytes() == payload
    assert published.manifest_path.exists() and published.sidecar_path.exists()
    assert attempt.record.state == "completed"
    assert not attempt.staging_dir.exists()


def test_publish_never_replaces_an_existing_final_path(in_memory_session_scope, storage_root):
    final = storage_root / "dumps" / "memory.dump"
    final.write_bytes(b"evidence already here")
    with pytest.raises(FileExistsError):
        with _begin(in_memory_session_scope, storage_root) as attempt:
            with attempt.open_payload() as handle:
                handle.write(b"newcomer")
            attempt.stage(_manifest)
            attempt.publish()

    assert final.read_bytes() == b"evidence already here"


def test_failure_mid_capture_removes_staging_and_fails_the_journal(in_memory_session_scope, storage_root):
    with pytest.raises(RuntimeError, match="capture died"):
        with _begin(in_memory_session_scope, storage_root) as attempt:
            staging = attempt.staging_dir
            with attempt.open_payload() as handle:
                handle.write(b"partial")
            raise RuntimeError("capture died")

    assert not staging.exists()
    assert not (storage_root / "dumps" / "memory.dump").exists()
    assert attempt.record.state == "failed"
    assert "capture died" in (attempt.record.error_summary or "")


def test_cleanup_failure_marks_cleanup_incomplete_and_records_an_event(
    in_memory_session_scope, storage_root, monkeypatch
):
    monkeypatch.setattr(
        publication, "_remove_staging", lambda *a, **k: (_ for _ in ()).throw(OSError("cleanup refused"))
    )
    with pytest.raises(RuntimeError, match="capture died"):
        with _begin(in_memory_session_scope, storage_root) as attempt:
            raise RuntimeError("capture died")

    assert attempt.record.state == "cleanup_incomplete"
    with in_memory_session_scope() as session:
        events = session.query(models.JobEvent).filter_by(job_id="job-1").all()
        assert any("cleanup" in event.message.lower() for event in events), "operator must see leftover bytes"


def test_cleanup_failure_does_not_mask_the_original_error(in_memory_session_scope, storage_root, monkeypatch):
    """The capture failure is the diagnosis; a cleanup error must not replace it."""
    monkeypatch.setattr(
        publication, "_remove_staging", lambda *a, **k: (_ for _ in ()).throw(OSError("cleanup refused"))
    )
    with pytest.raises(ValueError, match="the real problem"):
        with _begin(in_memory_session_scope, storage_root):
            raise ValueError("the real problem")


def test_states_advance_through_the_documented_sequence(in_memory_session_scope, storage_root):
    seen = []
    with _begin(in_memory_session_scope, storage_root) as attempt:
        seen.append(attempt.record.state)
        with attempt.open_payload() as handle:
            seen.append(attempt.record.state)
            handle.write(b"x")
        attempt.stage(_manifest)
        seen.append(attempt.record.state)
        attempt.publish()
        seen.append(attempt.record.state)
        with in_memory_session_scope() as session:
            attempt.complete(session)
        seen.append(attempt.record.state)
        attempt.finish()

    assert seen == ["reserved", "capturing", "staged", "published", "completed"]


def test_triad_is_fsynced_before_it_is_published(in_memory_session_scope, storage_root, monkeypatch):
    """A rename is only atomic against a crash if the bytes reached the platter."""
    synced = []
    real_fsync = os.fsync

    def counting_fsync(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting_fsync)

    with _begin(in_memory_session_scope, storage_root) as attempt:
        with attempt.open_payload() as handle:
            handle.write(b"bytes")
        attempt.stage(_manifest)
        before_publish = len(synced)
        attempt.publish()
        with in_memory_session_scope() as session:
            attempt.complete(session)
        attempt.finish()

    assert before_publish >= 3, "payload, manifest and sidecar each need an fsync before publication"


def test_manifest_write_failure_leaves_no_trace_at_the_destination(in_memory_session_scope, storage_root, monkeypatch):
    real_write = publication._write_verified

    def fail_on_manifest(directory_fd, name, payload):
        if name.endswith(".manifest.json"):
            raise OSError("manifest write refused")
        return real_write(directory_fd, name, payload)

    monkeypatch.setattr(publication, "_write_verified", fail_on_manifest)
    with pytest.raises(OSError, match="manifest write refused"):
        with _begin(in_memory_session_scope, storage_root) as attempt:
            staging = attempt.staging_dir
            with attempt.open_payload() as handle:
                handle.write(b"bytes")
            attempt.stage(_manifest)

    assert not staging.exists()
    assert list((storage_root / "dumps").iterdir()) == []
    assert attempt.record.state == "failed"


def test_sidecar_write_failure_leaves_no_trace_at_the_destination(in_memory_session_scope, storage_root, monkeypatch):
    real_write = publication._write_verified

    def fail_on_sidecar(directory_fd, name, payload):
        if name.endswith(".sha256"):
            raise OSError("sidecar write refused")
        return real_write(directory_fd, name, payload)

    monkeypatch.setattr(publication, "_write_verified", fail_on_sidecar)
    with pytest.raises(OSError, match="sidecar write refused"):
        with _begin(in_memory_session_scope, storage_root) as attempt:
            with attempt.open_payload() as handle:
                handle.write(b"bytes")
            attempt.stage(_manifest)

    assert list((storage_root / "dumps").iterdir()) == []
    assert attempt.record.state == "failed"


def test_a_link_failure_midway_rolls_back_the_partial_triad(in_memory_session_scope, storage_root, monkeypatch):
    """Two of three files in place is worse than none: the sidecar would attest
    bytes that are not there."""
    real_link = os.link
    calls = {"n": 0}

    def fail_on_second(source, destination, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("link refused")
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", fail_on_second)
    with pytest.raises(OSError, match="link refused"):
        with _begin(in_memory_session_scope, storage_root) as attempt:
            with attempt.open_payload() as handle:
                handle.write(b"bytes")
            attempt.stage(_manifest)
            attempt.publish()

    assert list((storage_root / "dumps").iterdir()) == [], "a partial triad must not survive"
    assert attempt.record.state == "failed"


def test_a_database_failure_after_publication_keeps_the_published_evidence(in_memory_session_scope, storage_root):
    """Once the bytes are at their final name they are evidence. A later database
    error fails the attempt for reconciliation to pick up - it does not delete them."""
    with pytest.raises(RuntimeError, match="database side failed"):
        with _begin(in_memory_session_scope, storage_root) as attempt:
            with attempt.open_payload() as handle:
                handle.write(b"bytes")
            attempt.stage(_manifest)
            published = attempt.publish()
            raise RuntimeError("database side failed")

    assert published.artifact_path.exists()
    assert published.manifest_path.exists()
    assert published.sidecar_path.exists()
    assert not attempt.staging_dir.exists()
    assert attempt.record.state == "failed"
    assert "database side failed" in (attempt.record.error_summary or "")


# ---------------------------------------------------------------------------
# Task 20 — adopting a file a tool already wrote
#
# Volatility writes its own outputs into the job's vol3 directory; AstinusLab
# does not choose where. The journal still records the publication and the
# manifest/sidecar pair still appears atomically.
# ---------------------------------------------------------------------------


def test_adopt_hashes_the_existing_file_and_stages_its_sidecars(in_memory_session_scope, storage_root):
    report = storage_root / "dumps" / "analysis-summary.json"
    report.write_text('{"status": "ok"}')

    with _begin(in_memory_session_scope, storage_root, name="analysis-summary.json") as attempt:
        triad = attempt.adopt(report, _manifest)

        assert triad.sha256 == hashlib.sha256(report.read_bytes()).hexdigest()
        assert triad.payload_path == report
        assert triad.manifest_path.parent == attempt.staging_dir
        assert not report.with_suffix(report.suffix + ".manifest.json").exists()
        attempt.abandon()


def test_publish_sidecars_links_both_or_neither(in_memory_session_scope, storage_root, monkeypatch):
    report = storage_root / "dumps" / "analysis-summary.json"
    report.write_text('{"status": "ok"}')

    real_link = os.link
    calls = {"n": 0}

    def fail_on_second(source, destination, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("link refused")
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", fail_on_second)

    with pytest.raises(OSError, match="link refused"):
        with _begin(in_memory_session_scope, storage_root, name="analysis-summary.json") as attempt:
            attempt.adopt(report, _manifest)
            attempt.publish_sidecars()

    assert report.exists(), "adoption must never remove the file it adopted"
    assert not report.with_suffix(report.suffix + ".manifest.json").exists()
    assert not report.with_suffix(report.suffix + ".sha256").exists()


def test_publish_sidecars_completes_the_pair_next_to_the_adopted_file(in_memory_session_scope, storage_root):
    report = storage_root / "dumps" / "analysis-summary.json"
    report.write_text('{"status": "ok"}')

    with _begin(in_memory_session_scope, storage_root, name="analysis-summary.json") as attempt:
        attempt.adopt(report, _manifest)
        published = attempt.publish_sidecars()
        with in_memory_session_scope() as session:
            attempt.complete(session)
        attempt.finish()

    assert published.artifact_path == report
    assert published.manifest_path.read_text().strip().startswith("{")
    assert published.sidecar_path.read_text().startswith(hashlib.sha256(b'{"status": "ok"}').hexdigest())
    assert attempt.record.state == "completed"
    assert not attempt.staging_dir.exists()


def test_adopt_refuses_a_symlink(in_memory_session_scope, storage_root):
    """A report path that is a symlink would let the manifest describe one file
    while the sidecars land beside another."""
    real = storage_root / "dumps" / "real.json"
    real.write_text("{}")
    link = storage_root / "dumps" / "link.json"
    link.symlink_to(real)

    with pytest.raises(OSError):
        with _begin(in_memory_session_scope, storage_root, name="link.json") as attempt:
            attempt.adopt(link, _manifest)


# ---------------------------------------------------------------------------
# Task 21 — startup reconciliation
#
# Every state the journal can be found in after a crash, and what recovery does
# with it. The rule that shapes all of them: never silently delete bytes, and
# never report an evidence file as complete without checking it.
# ---------------------------------------------------------------------------


def _crash_at(scope, storage_root, state, name="memory.dump", payload=b"bytes"):
    """Drive an attempt to `state` and walk away, as an interrupted worker would.

    The context manager is returned alongside the attempt and must stay
    referenced by the caller: dropping it lets the generator be finalised, which
    runs the very cleanup a crash never gets to run.
    """
    handle = _begin(scope, storage_root, name=name, kind="memory")
    attempt = handle.__enter__()
    if state in {"capturing", "staged", "published"}:
        with attempt.open_payload() as out:
            out.write(payload)
    if state in {"staged", "published"}:
        attempt.stage(
            lambda digest, size: build_artifact_manifest(
                artifact_path=Path(attempt.record.final_path),
                kind="memory",
                producer="job_capture",
                sha256=digest,
                size_bytes=size,
                job_id="job-1",
            )
        )
    if state == "published":
        attempt.publish()
    return attempt, handle


def test_reserved_attempt_with_no_bytes_is_closed(in_memory_session_scope, storage_root):
    attempt, _interrupted = _crash_at(in_memory_session_scope, storage_root, "reserved")
    staging = attempt.staging_dir

    report = publication.reconcile(in_memory_session_scope)

    assert attempt.record.state == "failed"
    assert not staging.exists()
    assert report.closed == 1


def test_capturing_attempt_leaves_no_partial_bytes_published(in_memory_session_scope, storage_root):
    attempt, _interrupted = _crash_at(in_memory_session_scope, storage_root, "capturing", payload=b"half")

    publication.reconcile(in_memory_session_scope)

    assert attempt.record.state == "failed"
    assert not (storage_root / "dumps" / "memory.dump").exists()
    assert not attempt.staging_dir.exists()


def test_staged_triad_is_quarantined_not_deleted(in_memory_session_scope, storage_root):
    """A complete triad with no database commit is evidence nobody claimed. It is
    moved somewhere an operator can find it, never removed."""
    attempt, _interrupted = _crash_at(in_memory_session_scope, storage_root, "staged")

    report = publication.reconcile(in_memory_session_scope, storage_root=storage_root)

    quarantine = storage_root / "quarantine" / attempt.id
    assert quarantine.is_dir()
    assert (quarantine / "payload").read_bytes() == b"bytes"
    assert (quarantine / "payload.manifest.json").exists()
    assert not (storage_root / "dumps" / "memory.dump").exists(), "quarantine must not publish"
    assert attempt.record.state == "failed"
    assert "quarantine" in (attempt.record.error_summary or "").lower()
    assert report.quarantined == 1


def _create_publication_job_owner(scope):
    with scope() as session:
        session.add(
            models.Job(
                id="job-1", target_id="fixture", scenario_id="fixture", execution_mode="immediate", status="completed"
            )
        )


def test_published_bytes_get_their_artifact_row_after_verification(in_memory_session_scope, storage_root):
    """Bytes at their final name with no row is the state an operator cannot
    explain. Recovery finishes the job the crash interrupted - but only after
    checking the file still hashes to what the journal recorded."""
    _create_publication_job_owner(in_memory_session_scope)
    attempt, _interrupted = _crash_at(in_memory_session_scope, storage_root, "published")

    report = publication.reconcile(in_memory_session_scope)

    with in_memory_session_scope() as session:
        artifact = session.query(models.Artifact).one()
        assert artifact.path == str(storage_root / "dumps" / "memory.dump")
        assert artifact.sha256 == hashlib.sha256(b"bytes").hexdigest()
        assert artifact.producer == "job_capture"
        assert "reconciled" in (artifact.metadata_json or "")

    assert attempt.record.state == "completed"
    assert report.completed == 1


def test_published_bytes_that_no_longer_match_are_an_integrity_failure(in_memory_session_scope, storage_root):
    attempt, _interrupted = _crash_at(in_memory_session_scope, storage_root, "published")
    (storage_root / "dumps" / "memory.dump").write_bytes(b"tampered")

    report = publication.reconcile(in_memory_session_scope)

    with in_memory_session_scope() as session:
        assert session.query(models.Artifact).count() == 0, "a mismatched file must not be adopted"

    assert attempt.record.state == "failed"
    summary = (attempt.record.error_summary or "").lower()
    assert "sha256" in summary or "integrity" in summary
    assert (storage_root / "dumps" / "memory.dump").read_bytes() == b"tampered", "recovery never deletes bytes"
    assert report.integrity_failures == 1


def test_completed_attempt_with_missing_bytes_is_reported_not_rewritten(in_memory_session_scope, storage_root):
    with _begin(in_memory_session_scope, storage_root) as attempt:
        _publish(attempt, in_memory_session_scope)
    (storage_root / "dumps" / "memory.dump").unlink()

    report = publication.reconcile(in_memory_session_scope)

    # History is not rewritten: the attempt did complete. The missing file is
    # surfaced instead.
    assert attempt.record.state == "completed"
    assert report.missing_evidence == [str(storage_root / "dumps" / "memory.dump")]


def test_reconcile_is_idempotent(in_memory_session_scope, storage_root):
    _create_publication_job_owner(in_memory_session_scope)
    _attempt, _interrupted = _crash_at(in_memory_session_scope, storage_root, "published")

    first = publication.reconcile(in_memory_session_scope)
    second = publication.reconcile(in_memory_session_scope)

    assert first.completed == 1
    assert second.completed == 0, "a second pass must find nothing left to do"
    with in_memory_session_scope() as session:
        assert session.query(models.Artifact).count() == 1


# ---------------------------------------------------------------------------
# Gate B regression — publishing a payload written by another user
#
# libvirt writes a core dump as root into the attempt directory. Ubuntu sets
# fs.protected_hardlinks=1, so the service user cannot hardlink a file it does
# not own: os.link fails with EPERM and every real memory capture failed at
# publication. Fake mode never sees it, because there the worker writes the
# payload itself.
# ---------------------------------------------------------------------------


def test_publish_survives_a_payload_the_worker_cannot_hardlink(in_memory_session_scope, storage_root, monkeypatch):
    def refuse_link(source, destination, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "link", refuse_link)

    with _begin(in_memory_session_scope, storage_root) as attempt:
        published = _publish(attempt, in_memory_session_scope, b"root-written dump")

    assert published.artifact_path.read_bytes() == b"root-written dump"
    assert published.manifest_path.exists() and published.sidecar_path.exists()
    assert attempt.record.state == "completed"


def test_the_fallback_still_refuses_to_replace_existing_evidence(in_memory_session_scope, storage_root, monkeypatch):
    """The fallback must keep the guarantee the link gave us."""

    def refuse_link(source, destination, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "link", refuse_link)
    final = storage_root / "dumps" / "memory.dump"
    final.write_bytes(b"evidence already here")

    with pytest.raises(FileExistsError):
        with _begin(in_memory_session_scope, storage_root) as attempt:
            with attempt.open_payload() as handle:
                handle.write(b"newcomer")
            attempt.stage(_manifest)
            attempt.publish()

    assert final.read_bytes() == b"evidence already here"


def test_rename_noreplace_refuses_a_taken_destination(tmp_path):
    """The syscall wrapper itself, independent of the publication flow."""
    source = tmp_path / "source"
    source.write_bytes(b"a")
    taken = tmp_path / "taken"
    taken.write_bytes(b"b")

    with pytest.raises(FileExistsError):
        publication._rename_noreplace(source, taken)

    assert taken.read_bytes() == b"b"
    assert source.exists()


def test_playground_published_crash_recovers_explicit_owner_and_custody(in_memory_session_scope, storage_root):
    scope = in_memory_session_scope
    with scope() as session:
        session.add(models.Case(id="case-recovered", name="Retained case"))
        session.add(models.PlaygroundSession(id="pg-recovered", status="destroyed"))
        session.flush()
        session.add(
            models.Acquisition(
                id="acq-recovered",
                name="Capture",
                status="running",
                case_id="case-recovered",
                session_id="pg-recovered",
                kind="memory",
            )
        )
    interrupted = _begin(
        scope,
        storage_root,
        producer="playground_capture",
        kind="memory",
        job_id=None,
        session_id="pg-recovered",
        acquisition_id="acq-recovered",
    )
    attempt = interrupted.__enter__()
    payload = b"original Playground capture bytes"
    with attempt.open_payload() as handle:
        handle.write(payload)
    attempt.stage(
        lambda sha256, size_bytes: build_artifact_manifest(
            artifact_path=Path(attempt.record.final_path),
            kind="memory",
            producer="playground_capture",
            sha256=sha256,
            size_bytes=size_bytes,
            session_id="pg-recovered",
            acquisition_id="acq-recovered",
        )
    )
    published = attempt.publish()
    original_manifest = published.manifest_path.read_bytes()
    original_sidecar = published.sidecar_path.read_bytes()

    report = publication.reconcile(scope)
    assert report.completed == 1
    with scope() as session:
        artifact = session.query(models.Artifact).one()
        assert artifact.session_id == "pg-recovered" and artifact.job_id is None
        assert artifact.acquisition_id == "acq-recovered" and artifact.case_id == "case-recovered"
        assert artifact.sha256 == hashlib.sha256(payload).hexdigest()
        assert artifact.path == str(published.artifact_path)
        acquisition = session.get(models.Acquisition, "acq-recovered")
        assert acquisition.status == "completed"
        assert acquisition.path == artifact.path
        assert acquisition.sha256 == artifact.sha256
        assert acquisition.size_bytes == artifact.size_bytes
        assert acquisition.sealed_at is not None
        assert acquisition.name == "Capture" and acquisition.case_id == "case-recovered"
        assert acquisition.job_vm_id is None
        assert (
            session.query(models.AcquisitionEvent).filter_by(acquisition_id=acquisition.id, status="completed").count()
            == 1
        )
        assert session.query(models.Job).count() == 0
    assert published.artifact_path.read_bytes() == payload
    assert published.manifest_path.read_bytes() == original_manifest
    assert published.sidecar_path.read_bytes() == original_sidecar
    assert publication.reconcile(scope).completed == 0
    with scope() as session:
        assert (
            session.query(models.AcquisitionEvent).filter_by(acquisition_id="acq-recovered", status="completed").count()
            == 1
        )


@pytest.mark.parametrize(
    "change",
    [
        "live",
        "failed",
        "cancelled",
        "sealed",
        "completed",
        "path",
        "sha256",
        "size_bytes",
        "session_id",
        "kind",
        "missing",
    ],
)
def test_recovery_refuses_conflicting_acquisition(in_memory_session_scope, storage_root, change):
    attempt, _interrupted = _producer_crash(in_memory_session_scope, storage_root, "playground_capture", "memory")
    with in_memory_session_scope() as session:
        acquisition = session.get(models.Acquisition, "acq-producer")
        if change == "missing":
            session.delete(acquisition)
        elif change in {"live", "failed", "cancelled", "sealed", "completed"}:
            acquisition.status = change
        else:
            setattr(acquisition, change, 999 if change == "size_bytes" else "conflicting")
    final = Path(attempt.record.final_path)
    original_bytes = final.read_bytes()
    report = publication.reconcile(in_memory_session_scope)
    assert report.completed == 0
    assert "reconciliation" in attempt.record.error_summary
    with in_memory_session_scope() as session:
        assert session.query(models.Artifact).count() == 0
        acquisition = session.get(models.Acquisition, "acq-producer")
        if acquisition:
            assert acquisition.sealed_at is None
            assert acquisition.status != "completed" or change == "completed"
    assert final.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "damage",
    [
        "missing_manifest",
        "missing_checksum",
        "invalid_json",
        "wrong_sha",
        "wrong_size",
        "boolean_size",
        "wrong_checksum",
        "symlink_manifest",
        "symlink_payload",
        "fifo_manifest",
        "fifo_payload",
        "extra_checksum",
        "duplicate_keys",
        "nan",
        "non_object",
        "missing_identity",
        "wrong_identity",
        "unknown_schema",
        "old_stub",
    ],
)
def test_recovery_refuses_damaged_triad_without_changing_bytes(
    in_memory_session_scope, storage_root, monkeypatch, damage
):
    _create_publication_job_owner(in_memory_session_scope)
    attempt, _interrupted = _crash_at(in_memory_session_scope, storage_root, "published")
    final = Path(attempt.record.final_path)
    with in_memory_session_scope() as session:
        row = session.get(models.ArtifactPublicationAttempt, attempt.id)
        manifest_path, checksum = Path(row.manifest_path), Path(row.sha256_sidecar_path)
    manifest = json.loads(manifest_path.read_text())
    if damage.startswith("missing_") and damage != "missing_identity":
        (manifest_path if damage == "missing_manifest" else checksum).unlink()
    elif damage == "invalid_json":
        manifest_path.write_text("{")
    elif damage in {"wrong_sha", "wrong_size", "boolean_size", "missing_identity", "wrong_identity", "unknown_schema"}:
        if damage == "missing_identity":
            manifest.pop("job_id")
        else:
            key, value = {
                "wrong_sha": ("sha256", "0" * 64),
                "wrong_size": ("size_bytes", 6),
                "boolean_size": ("size_bytes", True),
                "wrong_identity": ("job_id", "other"),
                "unknown_schema": ("schema_version", "unknown"),
            }[damage]
            manifest[key] = value
        manifest_path.write_text(json.dumps(manifest))
    elif damage in {"wrong_checksum", "extra_checksum"}:
        checksum.write_bytes(b"wrong\n" if damage == "wrong_checksum" else checksum.read_bytes() + b"extra")
    elif damage.startswith(("symlink_", "fifo_")):
        target = final if damage.endswith("payload") else manifest_path
        retained = target.with_name(target.name + ".retained")
        target.rename(retained)
        if damage.startswith("symlink_"):
            target.symlink_to(retained)
        else:
            os.mkfifo(target)
            original_open = os.open

            def guarded_open(path, flags, *args, **kwargs):
                if Path(path) == target:
                    assert flags & os.O_NONBLOCK, "a FIFO must not block before fstat"
                return original_open(path, flags, *args, **kwargs)

            monkeypatch.setattr(publication.os, "open", guarded_open)
    elif damage == "duplicate_keys":
        manifest_path.write_text(json.dumps(manifest)[:-1] + ', "producer": "job_capture"}')
    elif damage == "nan":
        manifest_path.write_text(json.dumps(manifest)[:-1] + ', "extra": NaN}')
    elif damage == "non_object":
        manifest_path.write_text("[]")
    elif damage == "old_stub":
        manifest_path.write_text(json.dumps(_manifest(manifest["sha256"], manifest["size_bytes"])))
    retained_bytes = {path: path.read_bytes() for path in final.parent.iterdir() if stat.S_ISREG(path.lstat().st_mode)}
    report = publication.reconcile(in_memory_session_scope)
    assert report.completed == 0
    assert report.integrity_failures == 1
    with in_memory_session_scope() as session:
        assert session.query(models.Artifact).count() == 0
        row = session.get(models.ArtifactPublicationAttempt, attempt.id)
        assert row.state == "failed" and "reconciliation" in row.error_summary
    assert all(path.read_bytes() == before for path, before in retained_bytes.items())


@pytest.mark.parametrize("overflow", [False, True])
def test_recovery_manifest_read_limit(in_memory_session_scope, storage_root, overflow):
    _create_publication_job_owner(in_memory_session_scope)
    attempt, _interrupted = _crash_at(in_memory_session_scope, storage_root, "published")
    with in_memory_session_scope() as session:
        path = Path(session.get(models.ArtifactPublicationAttempt, attempt.id).manifest_path)
    raw = path.read_bytes()
    path.write_bytes(raw + b" " * (16 * 1024 * 1024 + int(overflow) - len(raw)))
    report = publication.reconcile(in_memory_session_scope)
    assert report.completed == int(not overflow)
    assert path.stat().st_size == 16 * 1024 * 1024 + int(overflow)
    if overflow:
        assert "manifest exceeds recovery read limit" in attempt.record.error_summary


@pytest.mark.parametrize(
    "key", ["kind", "path", "filename", "sha256_sidecar_path", "job_id", "session_id", "acquisition_id", "producer"]
)
@pytest.mark.parametrize("missing", [False, True])
def test_recovery_requires_exact_manifest_identity(in_memory_session_scope, storage_root, key, missing):
    _create_publication_job_owner(in_memory_session_scope)
    attempt, _interrupted = _crash_at(in_memory_session_scope, storage_root, "published")
    with in_memory_session_scope() as session:
        path = Path(session.get(models.ArtifactPublicationAttempt, attempt.id).manifest_path)
    manifest = json.loads(path.read_text())
    if missing:
        manifest.pop(key)
    else:
        manifest[key] = "different"
    path.write_text(json.dumps(manifest))
    before = path.read_bytes()
    assert publication.reconcile(in_memory_session_scope).completed == 0
    assert path.read_bytes() == before


def _producer_crash(scope, root, producer, kind, *, adopt=False):
    job_id = session_id = acquisition_id = None
    with scope() as session:
        if producer == "playground_capture":
            session.add(models.PlaygroundSession(id="pg-producer", status="destroyed"))
            session.add(models.Case(id="case-producer", name="Retained case"))
            session.flush()
            session_id, acquisition_id = "pg-producer", "acq-producer"
            session.add(
                models.Acquisition(
                    id=acquisition_id,
                    name="Capture",
                    status="running",
                    kind=kind,
                    session_id=session_id,
                    case_id="case-producer",
                )
            )
        else:
            job_id = "job-1"
            session.add(
                models.Job(
                    id=job_id,
                    target_id="fixture",
                    scenario_id="fixture",
                    execution_mode="immediate",
                    status="completed",
                )
            )
    handle = _begin(
        scope, root, producer=producer, kind=kind, job_id=job_id, session_id=session_id, acquisition_id=acquisition_id
    )
    attempt = handle.__enter__()
    final = Path(attempt.record.final_path)

    def manifest(sha256, size_bytes):
        return build_artifact_manifest(
            artifact_path=final,
            kind=kind,
            producer=producer,
            sha256=sha256,
            size_bytes=size_bytes,
            job_id=job_id,
            session_id=session_id,
            acquisition_id=acquisition_id,
        )

    if adopt:
        final.write_bytes(b"producer bytes")
        attempt.adopt(final, manifest)
        attempt.publish_sidecars()
    else:
        with attempt.open_payload() as out:
            out.write(b"producer bytes")
        attempt.stage(manifest)
        attempt.publish()
    return attempt, handle


@pytest.mark.parametrize(
    ("producer", "kind"),
    [
        ("job_capture", "memory"),
        ("job_capture", "disk"),
        ("playground_capture", "memory"),
        ("playground_capture", "disk"),
        ("manual_upload", "memory"),
        ("vol3_analysis", "report"),
        ("vol3_plugin_run", "report"),
    ],
)
@pytest.mark.parametrize("adopt", [False, True])
def test_recovery_accepts_current_producer_manifests(in_memory_session_scope, storage_root, producer, kind, adopt):
    attempt, _interrupted = _producer_crash(in_memory_session_scope, storage_root, producer, kind, adopt=adopt)
    with in_memory_session_scope() as session:
        manifest = Path(session.get(models.ArtifactPublicationAttempt, attempt.id).manifest_path)
    value = json.loads(manifest.read_text())
    value.pop("integrity", None)  # Historical absence is documented and must remain supported.
    manifest.write_text(json.dumps(value))
    assert publication.reconcile(in_memory_session_scope).completed == 1
    assert publication.reconcile(in_memory_session_scope).completed == 0


@pytest.mark.parametrize(
    "field",
    [
        "producer",
        "kind",
        "sha256",
        "size_bytes",
        "job_id",
        "session_id",
        "acquisition_id",
        "filename",
        "manifest_path",
        "sha256_sidecar_path",
        "case_id",
    ],
)
def test_recovery_refuses_conflicting_existing_artifact(in_memory_session_scope, storage_root, field):
    attempt, _interrupted = _producer_crash(in_memory_session_scope, storage_root, "playground_capture", "memory")
    with in_memory_session_scope() as session:
        row = session.get(models.ArtifactPublicationAttempt, attempt.id)
        values = {
            key: getattr(row, key)
            for key in (
                "producer",
                "kind",
                "sha256",
                "size_bytes",
                "job_id",
                "session_id",
                "acquisition_id",
                "manifest_path",
                "sha256_sidecar_path",
            )
        }
        values.update(path=row.final_path, filename=Path(row.final_path).name, case_id="case-producer")
        values[field] = (
            999
            if field == "size_bytes"
            else None
            if field in {"case_id", "session_id", "acquisition_id"}
            else "different"
        )
        session.add(models.Artifact(id="existing", **values))
    assert publication.reconcile(in_memory_session_scope).completed == 0
    with in_memory_session_scope() as session:
        assert getattr(session.get(models.Artifact, "existing"), field) == values[field]
        assert session.get(models.Acquisition, "acq-producer").status == "running"


def test_recovery_bounds_growing_metadata_and_closes_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "manifest"
    path.write_bytes(b"{}")
    original_read, original_close = os.read, os.close
    total, opened, closed = [], [], []
    original_open = os.open

    def track_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def growing_read(fd, count):
        if fd not in opened:
            return original_read(fd, count)
        assert count <= publication._CHUNK
        total.append(count)
        return b" " * count

    def track_close(fd):
        closed.append(fd)
        original_close(fd)

    monkeypatch.setattr(publication.os, "open", track_open)
    monkeypatch.setattr(publication.os, "read", growing_read)
    monkeypatch.setattr(publication.os, "close", track_close)
    with pytest.raises(publication.PublicationIntegrityError, match="exceeds recovery read limit"):
        publication._read_recovery_metadata(path, "manifest", 2 * publication._CHUNK)
    assert sum(total) == 2 * publication._CHUNK + 1
    assert closed == opened


@pytest.mark.parametrize("fault", ["read", "parse", "swap"])
def test_recovery_faults_close_all_descriptors(in_memory_session_scope, storage_root, monkeypatch, fault):
    attempt, _interrupted = _crash_at(in_memory_session_scope, storage_root, "published")
    with in_memory_session_scope() as session:
        row = session.get(models.ArtifactPublicationAttempt, attempt.id)
        path = Path(row.manifest_path)
        original_open, original_close, original_read = os.open, os.close, os.read
        opened, closed, manifest_fds = [], [], []
        if fault == "parse":
            path.write_text("[")

        def track_open(name, *args, **kwargs):
            fd = original_open(name, *args, **kwargs)
            opened.append(fd)
            if Path(name) == path:
                manifest_fds.append(fd)
            return fd

        def track_close(fd):
            closed.append(fd)
            original_close(fd)

        swapped = False

        def faulty_read(fd, count):
            nonlocal swapped
            if fd in manifest_fds:
                if fault == "read":
                    raise OSError("injected read failure")
                if fault == "swap" and not swapped:
                    swapped = True
                    replacement = path.with_suffix(".replacement")
                    replacement.write_bytes(path.read_bytes())
                    replacement.replace(path)
            return original_read(fd, count)

        monkeypatch.setattr(publication.os, "open", track_open)
        monkeypatch.setattr(publication.os, "close", track_close)
        monkeypatch.setattr(publication.os, "read", faulty_read)
        with pytest.raises(publication.PublicationIntegrityError):
            publication._verify_published_triad(row)
        assert sorted(opened) == sorted(closed)
    _interrupted.__exit__(None, None, None)


@pytest.mark.parametrize("fault", ["artifact", "event", "journal"])
def test_capture_recovery_rolls_back_all_terminal_writes(in_memory_session_scope, storage_root, monkeypatch, fault):
    from astinuslab.db.repositories import ArtifactRepository

    attempt, interrupted = _producer_crash(in_memory_session_scope, storage_root, "playground_capture", "memory")
    original_transition = publication.transition
    scope = in_memory_session_scope
    with monkeypatch.context() as patch:
        if fault == "artifact":

            def fail_artifact(*args, **kwargs):
                raise RuntimeError("injected artifact failure")

            patch.setattr(ArtifactRepository, "create_artifact", fail_artifact)
        elif fault == "journal":

            def fail_transition(session, row, state):
                if state == "completed":
                    raise RuntimeError("injected journal failure")
                return original_transition(session, row, state)

            patch.setattr(publication, "transition", fail_transition)
        else:
            from sqlalchemy import event
            from sqlalchemy.orm import Session

            def fail_event(session, _context, _instances):
                if any(isinstance(row, models.AcquisitionEvent) and row.status == "completed" for row in session.new):
                    raise RuntimeError("injected event failure")

            event.listen(Session, "before_flush", fail_event)
        try:
            with pytest.raises(RuntimeError, match="injected"):
                publication.reconcile(scope)
        finally:
            if fault == "event":
                event.remove(Session, "before_flush", fail_event)
    with scope() as session:
        acquisition = session.get(models.Acquisition, "acq-producer")
        assert acquisition.status == "running" and acquisition.sealed_at is None
        assert acquisition.path is None
        assert session.query(models.AcquisitionEvent).filter_by(status="completed").count() == 0
        assert session.query(models.Artifact).count() == 0
        assert session.get(models.ArtifactPublicationAttempt, attempt.id).state == "published"
    assert Path(attempt.record.final_path).read_bytes() == b"producer bytes"
    assert publication.reconcile(scope).completed == 1
    interrupted.__exit__(None, None, None)


def test_capture_recovery_accepts_matching_sealed_result_without_another_event(in_memory_session_scope, storage_root):
    attempt, interrupted = _producer_crash(in_memory_session_scope, storage_root, "playground_capture", "memory")
    with in_memory_session_scope() as session:
        row = session.get(models.ArtifactPublicationAttempt, attempt.id)
        acquisition = session.get(models.Acquisition, "acq-producer")
        acquisition.status = "completed"
        acquisition.path, acquisition.sha256, acquisition.size_bytes = row.final_path, row.sha256, row.size_bytes
        acquisition.sealed_at = datetime.now(UTC)
        session.add(
            models.AcquisitionEvent(acquisition_id=acquisition.id, status="completed", message="Original completion")
        )
    assert publication.reconcile(in_memory_session_scope).completed == 1
    with in_memory_session_scope() as session:
        assert session.query(models.AcquisitionEvent).filter_by(status="completed").count() == 1
    interrupted.__exit__(None, None, None)


def test_capture_conditional_update_does_not_overwrite_competing_terminal_state(
    in_memory_session_scope, storage_root, monkeypatch
):
    from sqlalchemy import update
    from sqlalchemy.sql.dml import Update

    from astinuslab.db.repositories import AcquisitionRepository

    attempt, interrupted = _producer_crash(in_memory_session_scope, storage_root, "playground_capture", "memory")
    with in_memory_session_scope() as session:
        execute = session.execute
        competed = False

        def competing_execute(statement, *args, **kwargs):
            nonlocal competed
            if (
                isinstance(statement, Update)
                and getattr(statement.table, "name", None) == "acquisitions"
                and not competed
            ):
                competed = True
                execute(
                    update(models.Acquisition).where(models.Acquisition.id == "acq-producer").values(status="cancelled")
                )
            return execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", competing_execute)
        outcome = AcquisitionRepository(session).recover_playground_capture(
            acquisition_id="acq-producer",
            session_id="pg-producer",
            kind="memory",
            path=attempt.record.final_path,
            sha256=hashlib.sha256(b"producer bytes").hexdigest(),
            size_bytes=len(b"producer bytes"),
        )
        assert outcome == "conflict"
        acquisition = session.get(models.Acquisition, "acq-producer")
        assert acquisition.status == "cancelled" and acquisition.sealed_at is None
        assert session.query(models.AcquisitionEvent).count() == 0
    interrupted.__exit__(None, None, None)


@pytest.mark.parametrize("mode", ["staged", "hardlink", "rename", "adopt"])
def test_quarantine_describes_split_publication_locations(in_memory_session_scope, storage_root, mode):
    if mode == "adopt":
        interrupted = _begin(in_memory_session_scope, storage_root)
        attempt = interrupted.__enter__()
        final = Path(attempt.record.final_path)
        final.write_bytes(b"adopted payload")
        attempt.adopt(final, _manifest)
    else:
        attempt, interrupted = _crash_at(in_memory_session_scope, storage_root, "staged")
    final = Path(attempt.record.final_path)
    staged_names = [publication.PAYLOAD_NAME, "payload.manifest.json", "payload.sha256"]
    destinations = [
        final,
        final.with_suffix(final.suffix + ".manifest.json"),
        final.with_suffix(final.suffix + ".sha256"),
    ]
    if mode != "staged":
        for index, (name, destination) in enumerate(zip(staged_names, destinations, strict=True)):
            if mode == "adopt" and index == 0:
                continue
            source = attempt.staging_dir / name
            if index == 0 and mode in {"rename", "adopt"}:
                source.rename(destination)
            else:
                os.link(source, destination)
    retained = {path: path.read_bytes() for path in destinations if path.exists()}
    report = publication.reconcile(in_memory_session_scope, storage_root=storage_root)
    assert report.quarantined == 1 and report.completed == 0
    message = attempt.record.error_summary
    assert "remaining staging" in message
    assert "ownership is unresolved" in message
    assert str(final) in message and str(storage_root / "quarantine" / attempt.id) in message
    assert "never published" not in message and "complete triad" not in message
    assert all(path.read_bytes() == data for path, data in retained.items())
    assert publication.reconcile(in_memory_session_scope).quarantined == 0
    if retained:
        with pytest.raises(FileExistsError), _begin(in_memory_session_scope, storage_root) as retry:
            with retry.open_payload() as out:
                out.write(b"replacement")
            retry.stage(_manifest)
            retry.publish()
        assert all(path.read_bytes() == data for path, data in retained.items())
    interrupted.__exit__(None, None, None)
