"""Explicit Vol3 test support: synthetic inputs and complete subprocess output.

These helpers never execute Volatility or replace output/config validation.
Tests must explicitly replace their subprocess boundary with a synthetic child.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import ArtifactRepository, JobRepository
from astinuslab.forensics.analysis_input import SOURCE_SNAPSHOT_KEY, SourceSnapshot, select_source_artifact
from astinuslab.forensics.vol3_tools import VerifiedVolatility


def enqueue_pinned_analysis_action(repo, job_id, action_type, payload=None):
    """Create a current-format queued action without probing external tools."""
    source = select_source_artifact(repo.session, repo.get_job(job_id))
    return repo.enqueue_action(
        job_id,
        action_type,
        payload={**(payload or {}), SOURCE_SNAPSHOT_KEY: SourceSnapshot.of(source).to_payload()},
        source_artifact_id=source.id,
    )


def completed_child(cmd, streams, *, returncode=0, stdout=None, stderr=None):
    if isinstance(stdout, str):
        streams["stdout"].write(stdout)
    if isinstance(stderr, str):
        streams["stderr"].write(stderr)
    if returncode == 0 and "--save-config" in cmd:
        config = Path(cmd[cmd.index("--save-config") + 1])
        if not config.exists():
            symbols = Path(cmd[cmd.index("--symbol-dirs") + 1])
            staged = list((symbols / "linux").glob("*.json"))
            config.write_text(json.dumps({"kernel.isf_url": staged[0].resolve().as_uri()} if staged else {}))
    return SimpleNamespace(returncode=returncode)


def fixture_launcher(tmp_path):
    path = tmp_path / "fixture-vol3"
    path.write_text("#!/bin/sh\n# unit fixture; subprocess is replaced explicitly\n")
    return path


def make_completed_batch_job(
    in_memory_session_scope,
    tmp_path,
    *,
    with_dump: bool = True,
    with_isf: bool = True,
    with_action: bool = True,
    distro: str = "ubuntu",
):
    dump_path = tmp_path / "storage" / "dumps" / "memory.raw"
    if with_dump:
        dump_path.parent.mkdir(parents=True)
        dump_path.write_bytes(b"memdump")
    dump_sha256 = hashlib.sha256(dump_path.read_bytes()).hexdigest() if with_dump else None

    symbols_dir = tmp_path / "vol3" / "symbols"
    isf_path = symbols_dir / "linux" / "6.8.0-101-generic.json"
    if with_isf:
        isf_path.parent.mkdir(parents=True)
        isf_path.write_text('{"metadata":{"linux":{"name":"Linux"}},"symbols":{"init_task":{}}}', encoding="utf-8")
    isf_sha256 = hashlib.sha256(isf_path.read_bytes()).hexdigest() if with_isf else None

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        repo.force_transition(
            job.id,
            "completed",
            detected_distro=distro,
            detected_version="22.04",
            detected_kernel_release="6.8.0-101-generic",
            detected_architecture="x86_64",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        source_artifact = None
        if with_dump:
            source_artifact = ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path=str(dump_path),
                filename=dump_path.name,
                sha256=dump_sha256,
                size_bytes=dump_path.stat().st_size,
                job_id=job.id,
            )
        if with_isf:
            repo.create_isf_artifact(
                job.id,
                "6.8.0-101-generic",
                str(isf_path),
                "cache",
                distro=distro,
                version="22.04",
                sha256=isf_sha256,
                validation_status="valid",
                architecture="x86_64",
            )
        action = (
            repo.enqueue_action(
                job.id,
                "run_vol3_analysis",
                payload={SOURCE_SNAPSHOT_KEY: SourceSnapshot.of(source_artifact).to_payload()}
                if source_artifact
                else {},
                source_artifact_id=source_artifact.id if source_artifact else None,
            )
            if with_action
            else None
        )
        return job.id, action.id if action is not None else None, dump_path, isf_path


def patch_batch_settings(monkeypatch, tmp_path, plugins="linux.pslist.PsList,linux.bash.Bash"):
    import astinuslab.forensics.vol3_analysis as analysis

    monkeypatch.setattr(
        analysis,
        "settings",
        SimpleNamespace(
            storage_dir=tmp_path / "storage",
            vol3_symbols_path=tmp_path / "vol3" / "symbols",
            vol3_volatility_bin="/bin/vol3",
            vol3_default_plugins=plugins,
        ),
    )
    # No `raising=False`: a rename of the readiness seam must fail these tests loudly
    # instead of silently letting the real prober run against a fake settings object.
    launcher = fixture_launcher(tmp_path)
    monkeypatch.setattr(
        analysis,
        "require_verified_volatility",
        lambda _settings: verified_volatility(launcher),
    )
    return analysis


def verified_volatility(path: Path = Path("/verified/vol3")) -> VerifiedVolatility:
    """What the worker seam returns: a tool already proven ready, no optionals."""
    return VerifiedVolatility(path=path, version="review-readiness-version")


def patch_plugin_settings(monkeypatch, tmp_path, *, max_rows=50000):
    import astinuslab.forensics.vol3_plugin_run as runner

    monkeypatch.setattr(
        runner,
        "settings",
        SimpleNamespace(
            storage_dir=tmp_path / "storage",
            vol3_symbols_path=tmp_path / "vol3" / "symbols",
            vol3_volatility_bin="/bin/vol3",
            vol3_plugin_run_max_rows=max_rows,
        ),
    )
    # No `raising=False`: if the readiness seam is renamed again, these patches must
    # fail loudly rather than silently let the real prober run against a fake settings
    # object.
    launcher = fixture_launcher(tmp_path)
    monkeypatch.setattr(
        runner,
        "require_verified_volatility",
        lambda _settings: VerifiedVolatility(path=launcher, version="review-readiness-version"),
    )
    return runner


def make_plugin_job(
    scope,
    tmp_path,
    *,
    plugin,
    run_id,
    with_dump=True,
    with_isf=True,
    extra_payload=None,
    distro="ubuntu",
    record_dump_hash=False,
):
    dump_path = tmp_path / "storage" / "dumps" / "memory.lime"
    if with_dump:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_bytes(b"memdump")

    isf_path = tmp_path / "vol3" / "runs" / "pinrun1.work" / "symbols" / "linux" / "6.8.0-31-generic.json"
    if with_isf:
        isf_path.parent.mkdir(parents=True, exist_ok=True)
        isf_path.write_text('{"symbols":{}}', encoding="utf-8")

    with scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        repo.force_transition(
            job.id,
            "completed",
            detected_distro=distro,
            detected_version="22.04",
            detected_kernel_release="6.8.0-31-generic",
            detected_architecture="x86_64",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        source_artifact = None
        if with_dump:
            source_artifact = ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path=str(dump_path),
                filename=dump_path.name,
                sha256=hashlib.sha256(dump_path.read_bytes()).hexdigest() if record_dump_hash else None,
                job_id=job.id,
            )
        if with_isf:
            repo.create_isf_artifact(
                job.id,
                "6.8.0-31-generic",
                str(isf_path),
                "cache",
                distro=distro,
                validation_status="valid",
                architecture="x86_64",
            )
        payload = {"plugin": plugin, "run_id": run_id}
        if extra_payload:
            payload.update(extra_payload)
        if source_artifact is not None:
            payload[SOURCE_SNAPSHOT_KEY] = SourceSnapshot.of(source_artifact).to_payload()
        action = repo.enqueue_action(
            job.id,
            "run_vol3_plugin",
            payload=payload,
            source_artifact_id=source_artifact.id if source_artifact else None,
        )
        return job.id, action.id
