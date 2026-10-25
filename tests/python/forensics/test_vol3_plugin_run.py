from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from astinuslab.api.errors import ApiError
from astinuslab.api.schemas import JobCreate
from astinuslab.db.models import Artifact, ISFArtifact
from astinuslab.db.repositories import ArtifactRepository, JobRepository, ReportRepository
from astinuslab.forensics import services as forensic_services
from astinuslab.forensics.vol3_tools import ToolDependencyError, ToolStatus, VerifiedVolatility
from tests.python._support.helpers import autospec_setattr
from tests.python._support.vol3_fixtures import completed_child
from tests.python._support.vol3_fixtures import make_plugin_job as _make_job_with_action
from tests.python._support.vol3_fixtures import patch_plugin_settings as _patch_settings


def _completed_job(scope, *, distro="ubuntu"):
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
            detected_kernel_release="6.8.0-31-generic",
            detected_architecture="x86_64",
            finished_at=datetime.now(UTC),
        )
        ArtifactRepository(session).create_artifact(
            kind="memory",
            producer="job_capture",
            path="/tmp/dumps/memory.lime",
            filename="memory.lime",
            job_id=job.id,
        )
        return job.id


@pytest.fixture(autouse=True)
def _ready_enqueue_dependency(monkeypatch):
    monkeypatch.setattr(
        forensic_services,
        "check_volatility_tool",
        lambda _settings: ToolStatus(
            ready=True,
            code="ready",
            detail="Volatility 3 is ready.",
            version="2.28.0",
            resolved_path=Path("/verified/vol3"),
        ),
        raising=False,
    )


def test_run_vol3_plugin_serializes_under_job_lock(in_memory_session_scope, monkeypatch, tmp_path):
    from contextlib import contextmanager

    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="runlock1"
    )

    events: list[tuple[str, str | None]] = []

    @contextmanager
    def fake_lock(path):
        events.append(("lock", Path(path).name))
        yield
        events.append(("unlock", Path(path).name))

    monkeypatch.setattr(runner, "blocking_file_lock", fake_lock)

    def fake_run(cmd, **kwargs):
        events.append(("vol3", None))
        return completed_child(cmd, kwargs, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    success, _result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert events == [("lock", f"{job_id}.lock"), ("vol3", None), ("unlock", f"{job_id}.lock")]


def test_run_vol3_plugin_fresh_checks_and_uses_resolved_executable(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope,
        tmp_path,
        plugin="linux.pslist.PsList",
        run_id="freshpath1",
    )
    events: list[Any] = []
    resolved = tmp_path / "replacement-vol3"
    resolved.write_text("#!/bin/sh\n# replacement fixture\n")

    def require(_settings):
        events.append("require")
        return VerifiedVolatility(path=resolved, version="review-readiness-version")

    def fake_run(cmd, **_kwargs):
        events.append(("spawn", cmd))
        return completed_child(cmd, _kwargs, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(runner, "require_verified_volatility", require)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True, result
    # Exactly one verification, immediately before the spawn: probing is a full
    # framework load, so a redundant second probe is a real cost, not free safety.
    assert len(events) == 2
    assert events[0] == "require"
    assert events.count("require") == 1
    assert events[1][0] == "spawn"
    assert events[1][1][0] == str(resolved)
    assert "--version" not in events[1][1]


def test_run_vol3_plugin_removed_tool_fails_without_spawning(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope,
        tmp_path,
        plugin="linux.pslist.PsList",
        run_id="removed1",
    )
    monkeypatch.setattr(
        runner,
        "require_verified_volatility",
        lambda _settings: (_ for _ in ()).throw(
            ToolDependencyError("volatility_not_found", "The configured Volatility executable was not found.")
        ),
    )
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Volatility must not spawn when the queued tool was removed"),
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is False
    assert "Volatility executable was not found" in result["error"]


def test_run_vol3_plugin_revalidates_catalog_and_target_os_before_spawning(
    in_memory_session_scope,
    monkeypatch,
    tmp_path,
):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope,
        tmp_path,
        plugin="linux.pslist.PsList",
        run_id="replayed1",
        with_isf=False,
        distro="windows-11",
    )
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Volatility must not spawn for a replayed cross-OS plugin"),
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is False
    assert "linux.pslist.PsList" in result["error"]
    assert "windows" in result["error"]


def test_run_vol3_plugin_parses_json_into_table(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="run123"
    )

    rows_json = json.dumps(
        [
            {"PID": 1, "PPID": 0, "COMM": "systemd"},
            {"PID": 412, "PPID": 1, "COMM": "sshd"},
        ]
    )

    def fake_run(cmd, **kwargs):
        assert "-r" in cmd and "json" in cmd
        assert cmd[-1] == "linux.pslist.PsList"
        return completed_child(cmd, kwargs, returncode=0, stdout=rows_json, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert result["status"] == "completed"
    assert result["plugin"] == "linux.pslist.PsList"
    assert result["columns"] == ["PID", "PPID", "COMM"]
    assert result["rows"] == [[1, 0, "systemd"], [412, 1, "sshd"]]
    assert result["summary"] == "2 rows"
    assert result["dump_file"] == "memory.lime"

    run_file = tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / "run123.json"
    assert run_file.exists()
    assert json.loads(run_file.read_text())["columns"] == ["PID", "PPID", "COMM"]

    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == "completed"


def test_run_vol3_plugin_truncates_rows_over_cap(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path, max_rows=2)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="caprows1"
    )
    rows_json = json.dumps([{"PID": i, "COMM": f"p{i}"} for i in range(5)])

    def fake_run(*args, **kwargs):
        assert "stdout" in kwargs
        kwargs["stdout"].write(rows_json)
        return completed_child(args[0], kwargs, returncode=0, stdout=None, stderr=None)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert len(result["rows"]) == 2
    assert len(result["flags"]) == 2
    assert result["truncated"] is True
    assert result["total_rows"] == 5
    assert result["summary"] == "5 rows (showing first 2)"
    run_file = tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / "caprows1.json"
    raw_file = tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / "caprows1.stdout.json"
    assert len(json.loads(run_file.read_text(encoding="utf-8"))["rows"]) == 2
    assert len(json.loads(raw_file.read_text(encoding="utf-8"))) == 5


def test_run_vol3_plugin_findings_counted_before_truncation(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path, max_rows=1)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.malfind.Malfind", run_id="findcap1"
    )
    rows_json = json.dumps(
        [
            {"PID": 1, "Protection": "r--"},
            {"PID": 2, "Protection": "rw-"},
            {"PID": 3, "Protection": "rwx"},
        ]
    )
    autospec_setattr(
        monkeypatch,
        runner.subprocess,
        "run",
        lambda *a, **k: completed_child(a[0], k, returncode=0, stdout=rows_json, stderr=""),
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert len(result["rows"]) == 1
    assert result["findings"]["warn"] == 1
    assert result["total_rows"] == 3


def test_write_result_writes_meta_sidecar(tmp_path):
    import astinuslab.forensics.vol3_plugin_run as runner

    runner._write_result(
        tmp_path,
        {
            "run_id": "abc123",
            "job_id": "j",
            "plugin": "p",
            "dump_file": "d",
            "status": "completed",
            "elapsed": "1.0s",
            "finished_at": "x",
            "rows": [[1]] * 10,
            "columns": ["A"],
        },
    )

    meta = json.loads((tmp_path / "abc123.meta.json").read_text(encoding="utf-8"))
    assert set(meta) == set(runner._RUN_META_KEYS)
    assert meta["run_id"] == "abc123"
    assert "rows" not in meta


def test_run_vol3_plugin_reports_low_trust_isf_source(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="lowtrust1"
    )
    with in_memory_session_scope() as session:
        row = session.query(ISFArtifact).one()
        row.source = "live_guest"

    autospec_setattr(
        monkeypatch,
        runner.subprocess,
        "run",
        lambda *a, **k: completed_child(a[0], k, returncode=0, stdout=json.dumps([{"PID": 1}]), stderr=""),
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert result["isf_source"] == "live_guest"
    assert result["isf_low_trust"] is True
    run_file = tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / "lowtrust1.json"
    persisted = json.loads(run_file.read_text(encoding="utf-8"))
    assert persisted["isf_source"] == "live_guest"
    assert persisted["isf_low_trust"] is True


def test_run_vol3_plugin_fails_on_dump_hash_mismatch(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="hashbad1", record_dump_hash=True
    )
    with in_memory_session_scope() as session:
        row = session.query(Artifact).filter(Artifact.kind == "memory").one()
        Path(row.path).write_bytes(b"BADdump")

    autospec_setattr(
        monkeypatch,
        runner.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("vol3 must not run on mismatched evidence"),
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is False
    assert "hash mismatch" in result["error"]
    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == "failed"


def test_run_vol3_plugin_fails_on_isf_hash_mismatch(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="isfbad1"
    )
    with in_memory_session_scope() as session:
        row = session.query(ISFArtifact).one()
        row.sha256 = "0" * 64

    autospec_setattr(
        monkeypatch,
        runner.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("vol3 must not run on mismatched ISF evidence"),
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is False
    assert "ISF hash mismatch" in result["error"]
    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == "failed"


def test_run_vol3_plugin_verified_input_flag(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="verified1", record_dump_hash=True
    )

    autospec_setattr(
        monkeypatch,
        runner.subprocess,
        "run",
        lambda *a, **k: completed_child(a[0], k, returncode=0, stdout="[]", stderr=""),
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert result["input_verified"] is True
    run_file = tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / "verified1.json"
    persisted = json.loads(run_file.read_text(encoding="utf-8"))
    assert persisted["input_verified"] is True


def test_run_vol3_plugin_unrecorded_hash_is_unverified_but_runs(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="unver1"
    )
    autospec_setattr(
        monkeypatch,
        runner.subprocess,
        "run",
        lambda *a, **k: completed_child(a[0], k, returncode=0, stdout="[]", stderr=""),
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert result["input_verified"] is False


def test_run_vol3_plugin_cache_hit_inherits_live_guest_origin(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="cachelow1"
    )
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        cache = session.query(ISFArtifact).one()
        real_isf_sha256 = hashlib.sha256(Path(cache.path).read_bytes()).hexdigest()
        cache.source = "cache"
        cache.sha256 = real_isf_sha256
        origin = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        repo.create_isf_artifact(
            origin.id,
            "6.8.0-31-generic",
            cache.path,
            "live_guest",
            sha256=real_isf_sha256,
            validation_status="valid",
            architecture="x86_64",
        )

    autospec_setattr(
        monkeypatch,
        runner.subprocess,
        "run",
        lambda *a, **k: completed_child(a[0], k, returncode=0, stdout="[]", stderr=""),
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert result["isf_source"] == "live_guest"
    assert result["isf_low_trust"] is True


def test_run_vol3_plugin_trusted_source_not_flagged(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="trusted1"
    )
    with in_memory_session_scope() as session:
        row = session.query(ISFArtifact).one()
        row.source = "host_fetch"

    autospec_setattr(
        monkeypatch,
        runner.subprocess,
        "run",
        lambda *a, **k: completed_child(a[0], k, returncode=0, stdout="[]", stderr=""),
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert result["isf_source"] == "host_fetch"
    assert result["isf_low_trust"] is False


def test_parse_json_rows_file_flattens_children_tree(tmp_path):
    import astinuslab.forensics.vol3_plugin_run as runner

    output_path = tmp_path / "output.json"
    output_path.write_text(
        json.dumps(
            [
                {
                    "PID": 1,
                    "PPID": 0,
                    "COMM": "systemd",
                    "__children": [
                        {
                            "PID": 412,
                            "PPID": 1,
                            "COMM": "sshd",
                            "__children": [{"PID": 414, "PPID": 412, "COMM": "bash"}],
                        },
                        {"PID": 620, "PPID": 1, "COMM": "cron"},
                    ],
                },
                {"PID": 2, "PPID": 0, "COMM": "kthreadd"},
            ]
        ),
        encoding="utf-8",
    )
    columns, rows, total_rows, truncated = runner._parse_json_rows_file(output_path, max_rows=None)

    assert columns == ["Depth", "PID", "PPID", "COMM"]
    assert rows == [
        [0, 1, 0, "systemd"],
        [1, 412, 1, "sshd"],
        [2, 414, 412, "bash"],
        [1, 620, 1, "cron"],
        [0, 2, 0, "kthreadd"],
    ]
    assert total_rows == 5
    assert truncated is False


def test_parse_json_rows_file_flat_output_has_no_depth_column(tmp_path):
    import astinuslab.forensics.vol3_plugin_run as runner

    output_path = tmp_path / "output.json"
    output_path.write_text(
        json.dumps([{"PID": 1, "COMM": "systemd"}, {"PID": 2, "COMM": "kthreadd"}]),
        encoding="utf-8",
    )
    columns, rows, total_rows, truncated = runner._parse_json_rows_file(output_path, max_rows=None)

    assert columns == ["PID", "COMM"]
    assert rows == [[1, "systemd"], [2, "kthreadd"]]
    assert total_rows == 2
    assert truncated is False


def test_parse_json_rows_file_ignores_malformed_children(tmp_path):
    import astinuslab.forensics.vol3_plugin_run as runner

    output_path = tmp_path / "output.json"
    output_path.write_text(
        json.dumps(
            [
                {"PID": 1, "__children": "not-a-list"},
                {"PID": 2, "__children": [["not-a-dict"], {"PID": 3, "__children": None}]},
            ]
        ),
        encoding="utf-8",
    )
    columns, rows, total_rows, truncated = runner._parse_json_rows_file(output_path, max_rows=None)

    assert columns == ["Depth", "PID"]
    assert rows == [[0, 1], [0, 2], [1, 3]]
    assert total_rows == 3
    assert truncated is False


def test_run_vol3_plugin_pstree_returns_all_nested_rows(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pstree.PsTree", run_id="runtree1"
    )
    rows_json = json.dumps(
        [
            {
                "PID": 1,
                "PPID": 0,
                "COMM": "systemd",
                "__children": [
                    {
                        "PID": 412,
                        "PPID": 1,
                        "COMM": "sshd",
                        "__children": [{"PID": 414, "PPID": 412, "COMM": "bash"}],
                    },
                    {"PID": 620, "PPID": 1, "COMM": "cron"},
                ],
            },
            {"PID": 2, "PPID": 0, "COMM": "kthreadd"},
        ]
    )

    def fake_run(cmd, **kwargs):
        assert cmd[-1] == "linux.pstree.PsTree"
        return completed_child(cmd, kwargs, returncode=0, stdout=rows_json, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert result["columns"] == ["Depth", "PID", "PPID", "COMM"]
    assert len(result["rows"]) == 5
    assert result["summary"] == "5 rows"
    run_file = tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / "runtree1.json"
    persisted = json.loads(run_file.read_text(encoding="utf-8"))
    assert len(persisted["rows"]) == 5


def test_run_vol3_plugin_stages_yara_rules_and_appends_yara_file(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    rules = 'rule t { strings: $a = "root" condition: $a }'
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope,
        tmp_path,
        plugin="yarascan.YaraScan",
        run_id="yara1",
        extra_payload={"yara_rules": rules},
    )

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return completed_child(cmd, kwargs, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert result["status"] == "completed"
    rules_path = tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / "yara1.yar"
    assert rules_path.read_text(encoding="utf-8") == rules
    assert captured["cmd"][-3:] == ["yarascan.YaraScan", "--yara-file", str(rules_path)]


def test_run_vol3_plugin_rejects_yarascan_without_rules(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="yarascan.YaraScan", run_id="yaranone"
    )

    def explode(*_a, **_k):
        raise AssertionError("vol3 must not be invoked without rules")

    monkeypatch.setattr(runner.subprocess, "run", explode)

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is False
    assert "requires YARA rules" in result["error"]


def test_run_vol3_plugin_keeps_non_yara_command_unchanged(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="plain1"
    )
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return completed_child(cmd, kwargs, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert result["status"] == "completed"
    assert captured["cmd"][-1] == "linux.pslist.PsList"
    assert "--yara-file" not in captured["cmd"]
    assert not (tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / "plain1.yar").exists()


def test_run_vol3_plugin_records_failure_on_nonzero_exit(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="runfail"
    )

    def fake_run(cmd, **kwargs):
        return completed_child(cmd, kwargs, returncode=1, stdout="", stderr="symbol table not found")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is False
    assert result["status"] == "failed"
    assert "symbol table not found" in result["error"]
    run_file = tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / "runfail.json"
    assert run_file.exists()
    with in_memory_session_scope() as session:
        action = JobRepository(session).get_action(action_id)
        assert action is not None
        assert action.status == "failed"


def test_run_vol3_plugin_rejects_missing_plugin(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(in_memory_session_scope, tmp_path, plugin="", run_id="runnoplugin")

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is False
    assert "plugin" in result["error"].lower()


def test_run_vol3_plugin_finds_isf_built_under_other_job(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    # Dump job has no ISF of its own.
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="xjob", with_isf=False
    )
    # A valid ISF for the same kernel exists under a different (ISF-only) job.
    isf_path = tmp_path / "vol3" / "runs" / "pinrun1.work" / "symbols" / "linux" / "6.8.0-31-generic.json"
    isf_path.parent.mkdir(parents=True, exist_ok=True)
    isf_path.write_text('{"symbols":{}}', encoding="utf-8")
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        other = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        repo.create_isf_artifact(
            other.id,
            "6.8.0-31-generic",
            str(isf_path),
            "cache",
            distro="ubuntu",
            validation_status="valid",
            architecture="x86_64",
        )
    autospec_setattr(
        monkeypatch,
        runner.subprocess,
        "run",
        lambda *a, **k: completed_child(a[0], k, returncode=0, stdout="[]", stderr=""),
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert result["status"] == "completed"


def test_run_vol3_plugin_prefers_build_id_isf(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="bidrun1", with_isf=False
    )

    stale_isf = tmp_path / "vol3" / "symbols" / "linux" / "stale.json"
    current_isf = tmp_path / "vol3" / "symbols" / "linux" / "current.json"
    stale_isf.parent.mkdir(parents=True, exist_ok=True)
    stale_isf.write_text('{"symbols":{}}', encoding="utf-8")
    current_isf.write_text('{"symbols":{}}', encoding="utf-8")

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        other_job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        repo.create_isf_artifact(
            job_id,
            "6.8.0-31-generic",
            str(stale_isf),
            "cache",
            distro="ubuntu",
            validation_status="valid",
            architecture="x86_64",
        )
        repo.create_isf_artifact(
            other_job.id,
            "6.8.0-31-generic",
            str(current_isf),
            "host_fetch",
            distro="ubuntu",
            build_id="bb" * 8,
            validation_status="valid",
            architecture="x86_64",
        )
        repo.create_isf_artifact(
            job_id,
            "6.8.0-31-generic",
            str(tmp_path / "missing.json"),
            "host_fetch",
            distro="ubuntu",
            build_id="bb" * 8,
            validation_status="invalid",
            architecture="x86_64",
        )

    staged = []

    def fake_stage(output_dir, artifact_path):
        staged.append(artifact_path)
        symbols = output_dir / "symbols"
        (symbols / "linux").mkdir(parents=True, exist_ok=True)
        (symbols / "linux" / artifact_path.name).write_bytes(artifact_path.read_bytes())
        return symbols

    monkeypatch.setattr(runner, "_stage_selected_isf", fake_stage)
    autospec_setattr(
        monkeypatch,
        runner.subprocess,
        "run",
        lambda cmd, **kwargs: completed_child(cmd, kwargs, returncode=0, stdout="[]", stderr=""),
    )

    success, _result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert staged == [current_isf]


def test_run_vol3_plugin_records_pinned_isf(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="pinrun1"
    )
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        cfg = Path(cmd[cmd.index("--save-config") + 1])
        staged = Path(cmd[cmd.index("--symbol-dirs") + 1])
        isf = staged / "linux" / "6.8.0-31-generic.json"
        cfg.write_text(json.dumps({"kernel.symbol_table_name.isf_url": isf.resolve().as_uri()}), encoding="utf-8")
        return completed_child(cmd, kwargs, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert result["isf_pinned"] is True
    assert captured["cmd"][captured["cmd"].index("--cache-path") + 1] == str(
        tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / "pinrun1.work" / "vol3-cache"
    )
    staged_isf = (
        tmp_path
        / "storage"
        / "dumps"
        / job_id
        / "vol3"
        / "runs"
        / "pinrun1.work"
        / "symbols"
        / "linux"
        / "6.8.0-31-generic.json"
    )
    assert captured["cmd"][-5:] == [
        "-e",
        'plugins.PsList.kernel.symbol_table_name.class="volatility3.framework.symbols.linux.LinuxKernelIntermedSymbols"',
        "-e",
        f'plugins.PsList.kernel.symbol_table_name.isf_url="{staged_isf.resolve().as_uri()}"',
        "linux.pslist.PsList",
    ]


def test_run_vol3_plugin_fails_on_foreign_isf(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="pinbad1"
    )
    foreign = tmp_path / "global" / "evil.json"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("{}", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        cfg = Path(cmd[cmd.index("--save-config") + 1])
        cfg.write_text(json.dumps({"kernel.symbol_table_name.isf_url": foreign.resolve().as_uri()}), encoding="utf-8")
        return completed_child(cmd, kwargs, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is False
    assert "outside the staged symbols directory" in result["error"]


def test_run_vol3_plugin_rejects_missing_isf(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="runnoisf", with_isf=False
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is False
    assert "ISF" in result["error"]


def test_run_vol3_plugin_rejects_flaglike_plugin(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="--renderer=/etc/passwd", run_id="flagtest"
    )

    def explode(*_a, **_k):
        raise AssertionError("vol3 must not be invoked for a malformed plugin name")

    monkeypatch.setattr(runner.subprocess, "run", explode)

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is False
    assert "malformed plugin" in result["error"]


def test_run_vol3_plugin_sanitizes_traversal_run_id(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="../../evil"
    )
    autospec_setattr(
        monkeypatch,
        runner.subprocess,
        "run",
        lambda *a, **k: completed_child(a[0], k, returncode=0, stdout="[]", stderr=""),
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert "/" not in result["run_id"]
    assert not (tmp_path / "storage" / "dumps" / job_id / "vol3" / "evil.json").exists()


def test_run_vol3_plugin_attaches_severity_findings(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.malfind.Malfind", run_id="mal1"
    )
    rows_json = json.dumps(
        [
            {"PID": 8842, "Process": "miner", "Protection": "rwx"},
            {"PID": 1, "Process": "systemd", "Protection": "r--"},
        ]
    )
    autospec_setattr(
        monkeypatch,
        runner.subprocess,
        "run",
        lambda *a, **k: completed_child(a[0], k, returncode=0, stdout=rows_json, stderr=""),
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)

    assert success is True
    assert result["findings"] == {"crit": 0, "warn": 1}
    assert result["flags"][0] == "warn"
    assert result["flags"][1] is None


def test_run_vol3_plugin_publishes_report_artifact(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="rep1"
    )
    rows_json = json.dumps([{"PID": 1, "COMM": "systemd"}])
    autospec_setattr(
        monkeypatch,
        runner.subprocess,
        "run",
        lambda *a, **k: completed_child(a[0], k, returncode=0, stdout=rows_json, stderr=""),
    )

    success, result = runner.run_vol3_plugin(job_id, action_id=action_id)
    assert success is True

    run_file = tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / "rep1.json"
    manifest_file = run_file.with_suffix(".json.manifest.json")
    sidecar_file = run_file.with_suffix(".json.sha256")
    assert manifest_file.exists()
    assert sidecar_file.exists()

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "artifact-manifest/v1"
    assert manifest["producer"] == "vol3_plugin_run"
    assert manifest["kind"] == "report"
    assert manifest["job_id"] == job_id
    assert manifest["sha256"] in sidecar_file.read_text(encoding="utf-8")
    isf_input = next(item for item in manifest["inputs"] if item["kind"] == "isf")
    assert "source" in isf_input
    assert "pinned" in isf_input

    with in_memory_session_scope() as session:
        artifacts = [
            a for a in ArtifactRepository(session).list_artifacts(job_id=job_id) if a.producer == "vol3_plugin_run"
        ]
        assert len(artifacts) == 1
        art = artifacts[0]
        assert art.kind == "report"
        assert art.path == str(run_file)
        assert art.filename == "rep1.json"
        assert art.manifest_path == str(manifest_file)
        assert art.sha256_sidecar_path == str(sidecar_file)
        assert art.sha256 == manifest["sha256"]
        assert art.metadata_json is not None
        assert "isf_source" in json.loads(art.metadata_json)

        reports = ReportRepository(session).list_reports(job_id=job_id)
        rep = next(r for r in reports if r.report_type == "volatility3_plugin")
        assert rep.status == "completed"
        assert rep.path == str(run_file)
        assert rep.output_artifact_id == art.id


def test_run_vol3_plugin_failure_publishes_no_artifact(in_memory_session_scope, monkeypatch, tmp_path):
    runner = _patch_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "session_scope", in_memory_session_scope)
    job_id, action_id = _make_job_with_action(
        in_memory_session_scope, tmp_path, plugin="linux.pslist.PsList", run_id="repfail"
    )
    autospec_setattr(
        monkeypatch,
        runner.subprocess,
        "run",
        lambda *a, **k: completed_child(a[0], k, returncode=1, stdout="", stderr="boom"),
    )

    success, _ = runner.run_vol3_plugin(job_id, action_id=action_id)
    assert success is False

    run_file = tmp_path / "storage" / "dumps" / job_id / "vol3" / "runs" / "repfail.json"
    assert not run_file.with_suffix(".json.manifest.json").exists()
    with in_memory_session_scope() as session:
        from astinuslab.db.repositories import ArtifactRepository

        arts = [a for a in ArtifactRepository(session).list_artifacts(job_id=job_id) if a.producer == "vol3_plugin_run"]
        assert arts == []


# ── Enqueue service + concurrency guard ─────────────────────────────────────


def test_enqueue_vol3_plugin_run_returns_run(in_memory_session_scope):
    job_id = _completed_job(in_memory_session_scope)
    with in_memory_session_scope() as session:
        out = forensic_services.enqueue_vol3_plugin_run_service(session, job_id, "linux.pslist.PsList")
    assert out["status"] == "queued"
    assert out["run_id"]
    assert isinstance(out["action_id"], int)
    with in_memory_session_scope() as session:
        actions = [a for a in JobRepository(session).list_actions(job_id) if a.action_type == "run_vol3_plugin"]
        assert len(actions) == 1
        payload = actions[0].payload
        assert payload is not None
        assert json.loads(payload)["plugin"] == "linux.pslist.PsList"


def test_enqueue_rejects_unknown_plugin(in_memory_session_scope):
    job_id = _completed_job(in_memory_session_scope)
    with in_memory_session_scope() as session:
        with pytest.raises(ApiError) as exc:
            forensic_services.enqueue_vol3_plugin_run_service(session, job_id, "linux.bogus.Nope")
    assert exc.value.status_code == 400
    assert exc.value.code == "invalid_request"
    assert "linux.bogus.Nope" in exc.value.detail


def test_enqueue_plugin_readiness_failure_creates_no_action(in_memory_session_scope, monkeypatch):
    job_id = _completed_job(in_memory_session_scope)
    monkeypatch.setattr(
        forensic_services,
        "check_volatility_tool",
        lambda _settings: ToolStatus(
            ready=False,
            code="volatility_probe_failed",
            detail="The configured Volatility executable could not be probed.",
        ),
        raising=False,
    )

    with in_memory_session_scope() as session, pytest.raises(ApiError) as exc_info:
        forensic_services.enqueue_vol3_plugin_run_service(session, job_id, "linux.pslist.PsList")

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "dependency_unavailable"
    assert exc_info.value.context == {"dependency_code": "volatility_probe_failed"}
    with in_memory_session_scope() as session:
        actions = [a for a in JobRepository(session).list_actions(job_id) if a.action_type == "run_vol3_plugin"]
        assert actions == []


def test_enqueue_rejects_duplicate_running_plugin(in_memory_session_scope):
    job_id = _completed_job(in_memory_session_scope)
    with in_memory_session_scope() as session:
        forensic_services.enqueue_vol3_plugin_run_service(session, job_id, "linux.pslist.PsList")
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        action = next(a for a in repo.list_actions(job_id) if a.action_type == "run_vol3_plugin")
        repo.update_action(action.id, status="running")
    with in_memory_session_scope() as session:
        with pytest.raises(ApiError) as exc:
            forensic_services.enqueue_vol3_plugin_run_service(session, job_id, "linux.pslist.PsList")
    assert exc.value.status_code == 409
