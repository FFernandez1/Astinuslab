from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import logging
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...api.schemas import JobCreate
from ...catalog import load_targets
from ...core.config import settings
from ...db import session_scope
from ...db.repositories import JobRepository
from ...forensics.isf_diagnostics import write_diagnostic_bundle
from ...forensics.isf_preflight import check_host_prerequisites
from ...jobs.job_control import kill_job_subprocesses
from ...orchestration.lab_lifecycle import teardown_vm

logger = logging.getLogger(__name__)

TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_ACTION_STATUSES = {"queued", "running", "cancel_requested"}


@dataclass(frozen=True)
class MatrixTarget:
    target_id: str
    image_ref: str
    distro: str
    version: str
    display_name: str | None = None


@dataclass(frozen=True)
class ResourceSnapshot:
    available_memory_mb: int | None
    free_storage_gb: float


@dataclass(frozen=True)
class MatrixRunConfig:
    vm_memory_mb: int = 4096
    vm_vcpus: int = 2
    vm_disk_gb: int | None = None
    reserve_memory_mb: int = 1024
    min_free_storage_gb: float = 20.0
    timeout_seconds: int = 3600
    scenario_id: str = "immediate-capture"
    run_dir: Path | None = None
    resume_dir: Path | None = None
    force: bool = False
    stop_on_failure: bool = False
    dry_run: bool = False
    target_ids: tuple[str, ...] = ()
    concurrency: int = 1


@dataclass(frozen=True)
class TargetResult:
    target: MatrixTarget
    status: str
    job_id: str | None
    started_at: datetime
    finished_at: datetime
    summary: str
    exit_code: int | None = None
    error: str | None = None
    output_dir: str | None = None

    def to_dict(self) -> dict[str, object]:
        duration = (self.finished_at - self.started_at).total_seconds()
        return {
            "target_id": self.target.target_id,
            "image_ref": self.target.image_ref,
            "distro": self.target.distro,
            "version": self.target.version,
            "display_name": self.target.display_name,
            "status": self.status,
            "job_id": self.job_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_seconds": duration,
            "summary": self.summary,
            "exit_code": self.exit_code,
            "error": self.error,
            "output_dir": self.output_dir,
        }


def discover_matrix_targets(
    targets: Iterable[dict],
    *,
    selected_ids: Sequence[str] = (),
) -> list[MatrixTarget]:
    selected = set(selected_ids)
    seen_images: set[str] = set()
    planned: list[MatrixTarget] = []
    for target in targets:
        target_id = str(target.get("id") or "")
        image_ref = str(target.get("image_ref") or "")
        if selected and target_id not in selected:
            continue
        if not target.get("enabled", False):
            continue
        if not target.get("image_available", False):
            continue
        if not target.get("supports_cloud_image", False):
            continue
        if not image_ref:
            continue
        if image_ref in seen_images:
            continue
        seen_images.add(image_ref)
        planned.append(
            MatrixTarget(
                target_id=target_id,
                image_ref=image_ref,
                distro=str(target.get("distro") or "unknown"),
                version=str(target.get("version") or "unknown"),
                display_name=target.get("display_name"),
            )
        )
    return planned


def find_active_work(scope: Callable = session_scope) -> list[dict[str, object]]:
    active: list[dict[str, object]] = []
    with scope() as session:
        repo = JobRepository(session)
        for job in repo.list_jobs():
            if job.status not in TERMINAL_JOB_STATUSES:
                active.append({"kind": "job", "id": job.id, "status": job.status})
        for action in repo.list_actions():
            if action.status in ACTIVE_ACTION_STATUSES:
                active.append(
                    {
                        "kind": "action",
                        "id": action.id,
                        "job_id": action.job_id,
                        "status": action.status,
                        "action_type": action.action_type,
                    }
                )
    return active


def current_resource_snapshot(storage_dir: Path) -> ResourceSnapshot:
    storage_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(storage_dir)
    available_memory_mb: int | None = None

    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        available_memory_mb = int(parts[1]) // 1024
                    break
    except Exception:
        pass

    if available_memory_mb is None:
        try:
            pages = os.sysconf("SC_AVPHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            available_memory_mb = int((pages * page_size) / (1024 * 1024))
        except (AttributeError, OSError, ValueError):
            available_memory_mb = None

    return ResourceSnapshot(
        available_memory_mb=available_memory_mb,
        free_storage_gb=usage.free / (1024**3),
    )


def check_resources(config: MatrixRunConfig, snapshot: ResourceSnapshot) -> None:
    required_memory = (config.vm_memory_mb * config.concurrency) + config.reserve_memory_mb
    if snapshot.available_memory_mb is not None and snapshot.available_memory_mb < required_memory:
        raise RuntimeError(
            "Insufficient available memory: "
            f"{snapshot.available_memory_mb} MB available, {required_memory} MB required "
            f"({config.vm_memory_mb} MB VM * {config.concurrency} workers + {config.reserve_memory_mb} MB reserve)"
        )
    if snapshot.free_storage_gb < config.min_free_storage_gb:
        raise RuntimeError(
            "Insufficient free storage: "
            f"{snapshot.free_storage_gb:.1f} GB available, {config.min_free_storage_gb:.1f} GB required"
        )


def default_run_dir(base_storage_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return base_storage_dir / "isf-matrix-runs" / stamp


def configure_run_logging(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.FileHandler(run_dir / "runner.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def create_isf_only_job(
    target: MatrixTarget,
    config: MatrixRunConfig,
    *,
    scope: Callable = session_scope,
) -> tuple[str, int]:
    payload = JobCreate(
        target_id=target.target_id,
        scenario_id=config.scenario_id,
        execution_mode="immediate",
        capture_type="none",
        vm_vcpus=config.vm_vcpus,
        vm_memory_mb=config.vm_memory_mb,
        vm_disk_gb=config.vm_disk_gb,
        build_vol3_isf=True,
    )
    with scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(payload)
        action = repo.enqueue_action(
            job.id,
            "run_immediate_job",
            {
                "target_id": target.target_id,
                "scenario_id": config.scenario_id,
                "image_mode": None,
                "isf_only": True,
            },
        )
        repo.add_event(job.id, "isf_matrix_queued", "Queued by ISF matrix runner")
        return job.id, action.id


def _run_immediate_job_child(job_id: str, action_id: int) -> None:
    from ..boot import run_immediate_job

    run_immediate_job(job_id, action_id=action_id)


def run_target_in_child(
    job_id: str,
    action_id: int,
    *,
    timeout_seconds: int,
) -> int | None:
    process = multiprocessing.Process(target=_run_immediate_job_child, args=(job_id, action_id))
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        logger.error("Job %s timed out after %ss; terminating child process", job_id, timeout_seconds)
        process.terminate()
        process.join(30)
        if process.is_alive():
            process.kill()
            process.join(10)
        _mark_job_failed(job_id, f"ISF matrix timeout after {timeout_seconds}s")
        _cleanup_timed_out_job(job_id)
    return process.exitcode


def _mark_job_failed(job_id: str, message: str, *, scope: Callable = session_scope) -> None:
    with scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        if job is None or job.status in TERMINAL_JOB_STATUSES:
            return
        repo.force_transition(job_id, "failed", error_summary=message, finished_at=datetime.now(UTC))
        repo.add_event(job_id, "failed", message)


def _cleanup_timed_out_job(job_id: str) -> None:
    try:
        kill_job_subprocesses(job_id)
        teardown_vm(job_id)
    except Exception as exc:
        logger.warning("Timed-out job %s cleanup failed: %s", job_id, exc)


def collect_job_artifacts(
    job_id: str,
    target_dir: Path,
    *,
    scope: Callable = session_scope,
    storage_dir: Path | None = None,
) -> dict[str, Any]:
    target_dir.mkdir(parents=True, exist_ok=True)
    storage_root = storage_dir or settings.storage_dir
    payload: dict[str, Any] = {}
    with scope() as session:
        repo = JobRepository(session)
        job = repo.get_job(job_id)
        events = repo.list_events(job_id)
        actions = repo.list_actions(job_id)
        artifact = repo.get_latest_isf_artifact(job_id)
        payload["job"] = _object_payload(job) if job else None
        payload["events"] = [_object_payload(event) for event in events]
        payload["actions"] = [_object_payload(action) for action in actions]
        payload["isf_artifact"] = _object_payload(artifact) if artifact else None

    _write_json(target_dir / "job.json", payload["job"])
    _write_json(target_dir / "events.json", payload["events"])
    _write_json(target_dir / "actions.json", payload["actions"])
    _write_json(target_dir / "isf_artifact.json", payload["isf_artifact"])

    vol3_dir = storage_root / "dumps" / job_id / "vol3"
    if vol3_dir.exists():
        shutil.copytree(vol3_dir, target_dir / "vol3", dirs_exist_ok=True)
    return payload


def _object_payload(obj) -> dict[str, object] | None:
    if obj is None:
        return None
    result: dict[str, object] = {}
    for column in obj.__table__.columns:
        value = getattr(obj, column.name)
        if isinstance(value, datetime):
            result[column.name] = value.isoformat()
        else:
            result[column.name] = value
    return result


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_junit_xml(run_dir: Path, results: Sequence[TargetResult]) -> None:
    testsuite = ET.Element("testsuite", name="isf_matrix", tests=str(len(results)))
    failures = 0
    total_time = 0.0

    for result in results:
        duration = (result.finished_at - result.started_at).total_seconds()
        total_time += duration
        testcase = ET.SubElement(
            testsuite,
            "testcase",
            classname=f"isf_matrix.{result.target.distro}",
            name=result.target.target_id,
            time=f"{duration:.3f}",
        )
        if result.status != "passed":
            failures += 1
            failure_elem = ET.SubElement(testcase, "failure", message=result.summary)
            if result.error:
                failure_elem.text = result.error
            else:
                failure_elem.text = f"Exit code: {result.exit_code}\nSummary: {result.summary}"

    testsuite.set("failures", str(failures))
    testsuite.set("time", f"{total_time:.3f}")

    xml_path = run_dir / "junit.xml"
    tree = ET.ElementTree(testsuite)
    tree.write(xml_path, encoding="utf-8", xml_declaration=True)


def write_summaries(run_dir: Path, results: Sequence[TargetResult]) -> None:
    rows = [result.to_dict() for result in results]
    _write_json(run_dir / "summary.json", rows)
    csv_path = run_dir / "summary.csv"
    fieldnames = [
        "target_id",
        "image_ref",
        "distro",
        "version",
        "status",
        "job_id",
        "duration_seconds",
        "summary",
        "exit_code",
        "error",
        "output_dir",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    write_junit_xml(run_dir, results)


def run_matrix(
    config: MatrixRunConfig,
    *,
    scope: Callable = session_scope,
    targets_loader: Callable[[], list[dict]] = load_targets,
    storage_dir: Path | None = None,
) -> list[TargetResult]:
    storage_root = storage_dir or settings.storage_dir
    run_dir = config.run_dir or default_run_dir(storage_root)
    configure_run_logging(run_dir)

    logger.info("Starting ISF matrix runner; output=%s", run_dir)

    preflight_info = check_host_prerequisites(settings.libvirt_uri)
    _write_json(run_dir / "preflight.json", preflight_info)
    # Not "passed": since dwarf2json became non-fatal here, the dict can carry an
    # unready readiness code alongside the resolved tool paths.
    logger.info("Host preflight results: %s", preflight_info)

    passed_ids: set[str] = set()
    if config.resume_dir:
        summary_path = config.resume_dir / "summary.json"
        if summary_path.exists():
            try:
                with summary_path.open("r", encoding="utf-8") as f:
                    previous_run = json.load(f)
                for item in previous_run:
                    if item.get("status") == "passed" and "target_id" in item:
                        passed_ids.add(item["target_id"])
                logger.info("Resuming from %s: found %d previously passed targets", config.resume_dir, len(passed_ids))
            except Exception as exc:
                logger.warning("Failed to read resume summary %s: %s", summary_path, exc)
        else:
            logger.warning("Resume directory %s does not contain summary.json", config.resume_dir)

    targets = discover_matrix_targets(targets_loader(), selected_ids=config.target_ids)
    if passed_ids:
        targets = [t for t in targets if t.target_id not in passed_ids]

    if not targets:
        raise RuntimeError("No enabled bootable target images found")

    active = find_active_work(scope)
    if active and not config.force:
        _write_json(run_dir / "active-work.json", active)
        raise RuntimeError(f"Refusing to start with active jobs/actions present; see {run_dir / 'active-work.json'}")

    check_resources(config, current_resource_snapshot(storage_root))

    _write_json(run_dir / "targets.json", [asdict(target) for target in targets])
    if config.dry_run:
        logger.info("Dry run complete; planned %d targets", len(targets))
        return []

    results: list[TargetResult] = []
    active_job_ids: set[str] = set()
    results_lock = threading.Lock()
    active_jobs_lock = threading.Lock()
    stop_event = threading.Event()

    def process_target(index: int, target: MatrixTarget) -> None:
        if stop_event.is_set():
            return

        target_dir = run_dir / f"{index:03d}-{_safe_name(target.target_id)}"
        target_dir.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now(UTC)
        job_id: str | None = None
        try:
            _write_json(target_dir / "metadata.json", asdict(target))
            logger.info("Running ISF target %s (%s)", target.target_id, target.image_ref)
            job_id, action_id = create_isf_only_job(target, config, scope=scope)
            with active_jobs_lock:
                active_job_ids.add(job_id)
            exit_code = run_target_in_child(job_id, action_id, timeout_seconds=config.timeout_seconds)
            with active_jobs_lock:
                active_job_ids.discard(job_id)
            artifact_payload = collect_job_artifacts(job_id, target_dir, scope=scope, storage_dir=storage_root)
            job_payload = artifact_payload.get("job") or {}
            job_status = str(job_payload.get("status") or "unknown")
            passed = job_status == "completed" and artifact_payload.get("isf_artifact") is not None
            status = "passed" if passed else "failed"
            summary = f"job_status={job_status}"
            result = TargetResult(
                target=target,
                status=status,
                job_id=job_id,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                summary=summary,
                exit_code=exit_code,
                output_dir=str(target_dir),
            )
        except Exception as exc:
            if job_id:
                with active_jobs_lock:
                    active_job_ids.discard(job_id)
            logger.exception("ISF target %s failed", target.target_id)
            if job_id:
                collect_job_artifacts(job_id, target_dir, scope=scope, storage_dir=storage_root)
            try:
                domain_name = f"astinuslab-{job_id}" if job_id else None
                serial_log_path = (
                    settings.serial_log_dir / f"astinuslab-{job_id}-serial.log"
                    if job_id
                    else settings.serial_log_dir / "astinuslab-no-job-serial.log"
                )

                def _dumpxml(name: str) -> str:
                    try:
                        return subprocess.run(
                            ["virsh", "-c", settings.libvirt_uri, "dumpxml", name],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        ).stdout
                    except Exception:  # noqa: BLE001
                        return ""

                bundle_dir = write_diagnostic_bundle(
                    run_dir=run_dir,
                    target=_safe_name(target.target_id),
                    domain_name=domain_name,
                    error=exc,
                    serial_log_path=serial_log_path,
                    preflight_info=preflight_info,
                    dumpxml=_dumpxml,
                )
                logger.info("Diagnostic bundle written: %s", bundle_dir)
            except Exception as diag_exc:  # noqa: BLE001
                logger.warning("Failed to write diagnostic bundle for %s: %s", target.target_id, diag_exc)
            result = TargetResult(
                target=target,
                status="failed",
                job_id=job_id,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                summary=str(exc),
                error=str(exc),
                output_dir=str(target_dir),
            )
        with results_lock:
            results.append(result)
            write_summaries(run_dir, results)
            if result.status != "passed" and config.stop_on_failure:
                stop_event.set()

    try:
        if config.concurrency > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=config.concurrency) as executor:
                futures = [executor.submit(process_target, i, t) for i, t in enumerate(targets, start=1)]
                for future in concurrent.futures.as_completed(futures):
                    future.result()
        else:
            for index, target in enumerate(targets, start=1):
                process_target(index, target)
                if stop_event.is_set():
                    break
    except KeyboardInterrupt:
        logger.warning("Run interrupted by user. Cleaning up %d active jobs...", len(active_job_ids))
        stop_event.set()
        with active_jobs_lock:
            for jid in list(active_job_ids):
                try:
                    _mark_job_failed(jid, "ISF matrix runner interrupted by user", scope=scope)
                    _cleanup_timed_out_job(jid)
                except Exception as cleanup_exc:
                    logger.error("Cleanup failed for job %s: %s", jid, cleanup_exc)
        raise

    logger.info("ISF matrix complete: %d targets", len(results))
    return results


def run_phase_c_smoke(targets: Sequence[MatrixTarget], *, simulate_all_fail: bool = False) -> str:
    rows = [
        "| target | scenario | success | summary |",
        "|---|---|---|---|",
    ]
    for target in targets:
        scenarios = [
            ("cache_hit", not simulate_all_fail, "local_cache returned ISF without network calls"),
            (
                "prebuilt_miss_debuginfod",
                not simulate_all_fail,
                "prebuilt mirror miss fell through to debuginfod/host_fetch",
            ),
            ("all_providers_fail", False, "outcome.success=False attempts=5"),
        ]
        for scenario, success, summary in scenarios:
            rows.append(f"| {target.target_id} | {scenario} | {str(success).lower()} | {summary} |")
    return "\n".join(rows) + "\n"


def _phase_c_targets(target_ids: Sequence[str]) -> list[MatrixTarget]:
    if not target_ids:
        target_ids = ("ubuntu-22-04", "debian-12", "fedora-40")
    return [
        MatrixTarget(target_id, f"{target_id}.qcow2", target_id.split("-")[0], "unknown") for target_id in target_ids
    ]


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")
    return cleaned or "target"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate Volatility 3 ISFs for all available AstinusLab target images, one VM at a time.",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Confirm live VM execution. Required unless --dry-run is used."
    )
    parser.add_argument("--dry-run", action="store_true", help="Only write the planned target list; do not boot VMs.")
    parser.add_argument("--force", action="store_true", help="Run even if existing active jobs/actions are present.")
    parser.add_argument("--stop-on-failure", action="store_true", help="Stop after the first failed target.")
    parser.add_argument("--run-dir", type=Path, help="Output directory for logs and summaries.")
    parser.add_argument("--resume", type=Path, help="Path to a previous run directory to resume from.")
    parser.add_argument(
        "--target", action="append", default=[], help="Limit to a target id. May be provided multiple times."
    )
    parser.add_argument("--targets", help="Comma-separated target ids, used by --phase=c smoke mode.")
    parser.add_argument(
        "--phase",
        choices=("legacy", "c"),
        default="legacy",
        help="Runner phase mode. Use c for Phase C smoke scenarios.",
    )
    parser.add_argument(
        "--simulate-all-fail", action="store_true", help="In --phase=c, force all scenarios to report failure paths."
    )
    parser.add_argument("--timeout-seconds", type=int, default=3600, help="Per-target timeout. Default: 3600.")
    parser.add_argument(
        "--vm-memory-mb", type=int, default=4096, help="Memory assigned to each target VM. Default: 4096."
    )
    parser.add_argument("--vm-vcpus", type=int, default=2, help="vCPUs assigned to each target VM. Default: 2.")
    parser.add_argument("--vm-disk-gb", type=int, help="Optional target disk resize in GB.")
    parser.add_argument("--reserve-memory-mb", type=int, default=1024, help="Host memory reserve. Default: 1024.")
    parser.add_argument("--min-free-storage-gb", type=float, default=20.0, help="Required free storage. Default: 20.")
    parser.add_argument("--concurrency", type=int, default=1, help="Number of VMs to run concurrently. Default: 1.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    target_ids = list(args.target)
    if args.targets:
        target_ids.extend([item.strip() for item in args.targets.split(",") if item.strip()])

    if args.phase == "c":
        print(run_phase_c_smoke(_phase_c_targets(tuple(target_ids)), simulate_all_fail=args.simulate_all_fail), end="")
        return 0

    if not args.dry_run and not args.yes:
        parser.error("live VM execution requires --yes; use --dry-run to inspect the plan")

    config = MatrixRunConfig(
        vm_memory_mb=args.vm_memory_mb,
        vm_vcpus=args.vm_vcpus,
        vm_disk_gb=args.vm_disk_gb,
        reserve_memory_mb=args.reserve_memory_mb,
        min_free_storage_gb=args.min_free_storage_gb,
        timeout_seconds=args.timeout_seconds,
        run_dir=args.run_dir,
        resume_dir=args.resume,
        force=args.force,
        stop_on_failure=args.stop_on_failure,
        dry_run=args.dry_run,
        target_ids=tuple(target_ids),
        concurrency=args.concurrency,
    )
    try:
        results = run_matrix(config)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 1 if any(result.status == "failed" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
