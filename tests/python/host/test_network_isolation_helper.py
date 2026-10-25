"""Network isolation helper validation, rule ordering, and failure recovery.

Rule operations use a temporary fake iptables executable, never host rules.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[3]
HELPER = ROOT / "ops" / "host" / "network_isolation_admin.sh"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    return subprocess.run(
        ["bash", str(HELPER), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_helper_exists_and_is_executable():
    assert HELPER.exists(), HELPER
    assert os.access(HELPER, os.X_OK)


def test_noop_succeeds():
    result = _run(["noop"])
    assert result.returncode == 0, result.stderr


def test_unknown_subcommand_rejected():
    result = _run(["bogus"])
    assert result.returncode != 0
    assert "Unsupported subcommand" in result.stderr


def test_apply_requires_minimum_args():
    result = _run(["apply", "job-x"])
    assert result.returncode != 0
    assert "Usage" in result.stderr


def test_remove_requires_minimum_args():
    result = _run(["remove"])
    assert result.returncode != 0
    assert "Usage" in result.stderr


def test_apply_rejects_invalid_job_id():
    result = _run(["apply", "../etc/passwd", "192.168.1.10"])
    assert result.returncode != 0
    assert "invalid job_id" in result.stderr


def test_apply_rejects_overlong_job_id():
    result = _run(["apply", "x" * 65, "192.168.1.10"])
    assert result.returncode != 0
    assert "invalid job_id" in result.stderr


def test_apply_rejects_invalid_ipv4_format():
    result = _run(["apply", "job-abc", "not-an-ip"])
    assert result.returncode != 0
    assert "invalid guest IPv4" in result.stderr


def test_apply_rejects_ipv4_octet_above_255():
    result = _run(["apply", "job-abc", "999.1.1.1"])
    assert result.returncode != 0
    assert "octet > 255" in result.stderr or "invalid guest IPv4" in result.stderr


def test_apply_rejects_invalid_attacker_ipv4():
    result = _run(["apply", "job-abc", "192.168.1.10", "bad-ip"])
    assert result.returncode != 0
    assert "invalid attacker IPv4" in result.stderr


def test_remove_rejects_invalid_job_id():
    result = _run(["remove", "job/with/slash", "192.168.1.10"])
    assert result.returncode != 0
    assert "invalid job_id" in result.stderr


def test_apply_rejects_too_many_args():
    result = _run(["apply", "job-abc", "192.168.1.10", "10.0.0.1", "extra"])
    assert result.returncode != 0
    assert "Usage" in result.stderr


def test_remove_rejects_too_many_args():
    result = _run(["remove", "job-abc", "192.168.1.10", "10.0.0.1", "extra"])
    assert result.returncode != 0
    assert "Usage" in result.stderr


def _write_mock_iptables(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "rules.txt"
    log = tmp_path / "iptables.log"
    state.write_text("")
    log.write_text("")
    iptables = bin_dir / "iptables"
    iptables.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$IPTABLES_LOG"
cmd="$1"
chain="$2"
shift 2
case "$cmd" in
  -C)
    line="$chain $*"
    if [[ -n "${IPTABLES_CHECK_EXIT:-}" && "$line" == *"${IPTABLES_CHECK_COMMENT:-}" ]]; then
      touch "${IPTABLES_STATE}.query-failed"
      echo "injected query failure" >&2
      exit "$IPTABLES_CHECK_EXIT"
    fi
    grep -Fxq -- "$line" "$IPTABLES_STATE"
    ;;
  -S)
    if [[ -n "${IPTABLES_LIST_EXIT:-}" && "${IPTABLES_LIST_EXIT}" != 0 && -f "${IPTABLES_STATE}.query-failed" ]]; then
      echo "injected table access failure" >&2
      exit "$IPTABLES_LIST_EXIT"
    fi
    awk -v chain="$chain" -v quote="${IPTABLES_QUOTE_COMMENTS:-0}" '
      $1 == chain {
        if (quote == 1) {
          for (i = 1; i < NF; i++) if ($i == "--comment") $(i + 1) = sprintf("%c%s%c", 34, $(i + 1), 34)
        }
        print "-A " $0
      }' "$IPTABLES_STATE"
    ;;
  -I)
    pos="$1"
    shift
    line="$chain $*"
    if [[ "${IPTABLES_FAIL_COMMENT:-}" != "" && "$line" == *"$IPTABLES_FAIL_COMMENT" ]]; then
      echo "injected insertion failure" >&2
      exit 27
    fi
    tmp="${IPTABLES_STATE}.tmp"
    awk -v line="$line" -v pos="$pos" 'NR == pos { print line; inserted=1 } { print } END { if (!inserted) print line }' "$IPTABLES_STATE" > "$tmp"
    mv "$tmp" "$IPTABLES_STATE"
    ;;
  -D)
    line="$chain $*"
    if [[ -n "${IPTABLES_DELETE_COMMENT:-}" && "$line" == *"$IPTABLES_DELETE_COMMENT" ]]; then
      echo "injected deletion failure" >&2
      exit 4
    fi
    tmp="${IPTABLES_STATE}.tmp"
    awk -v line="$line" 'BEGIN { removed=0 } $0 == line && removed == 0 { removed=1; next } { print }' "$IPTABLES_STATE" > "$tmp"
    mv "$tmp" "$IPTABLES_STATE"
    ;;
  *)
    echo "unexpected iptables command: $cmd" >&2
    exit 2
    ;;
esac
"""
    )
    iptables.chmod(0o755)
    return state, log


def _run_with_mock_iptables(args: list[str], tmp_path: Path) -> tuple[subprocess.CompletedProcess, Path, Path]:
    state, log = _write_mock_iptables(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path / 'bin'}:{env['PATH']}"
    env["IPTABLES_STATE"] = str(state)
    env["IPTABLES_LOG"] = str(log)
    result = subprocess.run(
        ["bash", str(HELPER), *args],
        capture_output=True,
        text=True,
        env=env,
    )
    return result, state, log


def test_apply_uses_exact_idempotent_iptables_rules(tmp_path):
    result, state, log = _run_with_mock_iptables(["apply", "job-abc", "192.168.122.10", "10.0.0.5"], tmp_path)
    assert result.returncode == 0, result.stderr

    result = subprocess.run(
        ["bash", str(HELPER), "apply", "job-abc", "192.168.122.10", "10.0.0.5"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "IPTABLES_STATE": str(state),
            "IPTABLES_LOG": str(log),
        },
    )
    assert result.returncode == 0, result.stderr

    assert state.read_text().splitlines() == _ordered_rules()
    assert _verdict(state, "192.168.122.10", "10.0.0.5") == "ACCEPT"
    assert _verdict(state, "10.0.0.5", "192.168.122.10") == "ACCEPT"
    assert _verdict(state, "192.168.122.10", "8.8.8.8") == "DROP"


def _ordered_rules():
    return [
        "FORWARD -s 192.168.122.10 -d 10.0.0.5 -j ACCEPT -m comment --comment astinuslab-isolate-job-abc-att-fwd",
        "FORWARD -s 10.0.0.5 -d 192.168.122.10 -j ACCEPT -m comment --comment astinuslab-isolate-job-abc-att-rev",
        "FORWARD -s 192.168.122.10 -j DROP -m comment --comment astinuslab-isolate-job-abc",
    ]


def _verdict(state, source, destination):
    for line in state.read_text().splitlines():
        rule = line.split()
        if "-s" in rule and rule[rule.index("-s") + 1] != source:
            continue
        if "-d" in rule and rule[rule.index("-d") + 1] != destination:
            continue
        return rule[rule.index("-j") + 1]
    return "POLICY"


@pytest.mark.parametrize("prior", [[], [2], [0], [1], [0, 1], [2, 0, 1], [0, 0, 2, 1]])
def test_apply_repairs_partial_or_legacy_order_and_preserves_unrelated_rules(tmp_path, prior):
    state, log = _write_mock_iptables(tmp_path)
    unrelated = "FORWARD -s 10.8.0.1 -j DROP -m comment --comment another-owner"
    state.write_text("\n".join([*[_ordered_rules()[i] for i in prior], unrelated]) + "\n")
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "IPTABLES_STATE": str(state),
        "IPTABLES_LOG": str(log),
    }
    for _ in range(2):
        result = subprocess.run(
            ["bash", str(HELPER), "apply", "job-abc", "192.168.122.10", "10.0.0.5"],
            env=env,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert state.read_text().splitlines() == _ordered_rules() + [unrelated]
        assert _verdict(state, "192.168.122.10", "10.0.0.5") == "ACCEPT"
        assert _verdict(state, "10.0.0.5", "192.168.122.10") == "ACCEPT"
        assert _verdict(state, "192.168.122.10", "8.8.8.8") == "DROP"
    result = subprocess.run(
        ["bash", str(HELPER), "remove", "job-abc", "192.168.122.10", "10.0.0.5"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert state.read_text().splitlines() == [unrelated]


def test_apply_failure_retains_deny_and_reapply_recovers(tmp_path):
    state, log = _write_mock_iptables(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "IPTABLES_STATE": str(state),
        "IPTABLES_LOG": str(log),
    }
    args = ["bash", str(HELPER), "apply", "job-abc", "192.168.122.10", "10.0.0.5"]
    result = subprocess.run(args, env={**env, "IPTABLES_FAIL_COMMENT": "-att-fwd"}, capture_output=True)
    assert result.returncode != 0
    assert _verdict(state, "192.168.122.10", "8.8.8.8") == "DROP"
    result = subprocess.run(args, env=env, capture_output=True)
    assert result.returncode == 0
    assert state.read_text().splitlines() == _ordered_rules()


def test_remove_cleans_all_expected_iptables_rules(tmp_path):
    result, state, _log = _run_with_mock_iptables(["apply", "job-abc", "192.168.122.10", "10.0.0.5"], tmp_path)
    assert result.returncode == 0, result.stderr

    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "IPTABLES_STATE": str(state),
        "IPTABLES_LOG": str(tmp_path / "iptables.log"),
    }
    result = subprocess.run(
        ["bash", str(HELPER), "remove", "job-abc", "192.168.122.10", "10.0.0.5"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert state.read_text().splitlines() == []


@pytest.mark.parametrize("command", ["apply", "remove"])
@pytest.mark.parametrize("error", [1, 2, 4, 127])
@pytest.mark.parametrize("comment", ["", "-att-rev"], ids=["first-query", "later-query"])
def test_query_errors_abort_without_treating_rule_as_absent(
    tmp_path: Path, command: str, error: int, comment: str
) -> None:
    state, log = _write_mock_iptables(tmp_path)
    unrelated = "FORWARD -s 10.8.0.1 -j DROP -m comment --comment another-owner"
    before = _ordered_rules() + [unrelated]
    state.write_text("\n".join(before) + "\n")
    result = subprocess.run(
        ["bash", str(HELPER), command, "job-abc", "192.168.122.10", "10.0.0.5"],
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "IPTABLES_STATE": str(state),
            "IPTABLES_LOG": str(log),
            "IPTABLES_CHECK_EXIT": str(error),
            "IPTABLES_CHECK_COMMENT": comment,
            "IPTABLES_LIST_EXIT": "1",
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == error, result.stderr
    # A later remove query can fail after DROP removal. That partial cleanup
    # must still fail so custody survives for the remaining exceptions.
    expected = before if command == "apply" or not comment else before[:2] + [unrelated]
    assert state.read_text().splitlines() == expected
    mutations = [line for line in log.read_text().splitlines() if not line.startswith(("-C ", "-S "))]
    assert mutations == ([] if expected == before else [f"-D {_ordered_rules()[2]}"])


def test_absent_rules_are_successful_without_deletion(tmp_path: Path) -> None:
    result, state, log = _run_with_mock_iptables(["remove", "job-abc", "192.168.122.10", "10.0.0.5"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert state.read_text() == ""
    operations = log.read_text().splitlines()
    assert len(operations) == 6
    assert all(line.startswith("-C FORWARD ") for line in operations[::2])
    assert operations[1::2] == ["-S FORWARD"] * 3


@pytest.mark.parametrize("command", ["apply", "remove"])
@pytest.mark.parametrize("quoted", [False, True], ids=["unquoted", "quoted"])
@pytest.mark.parametrize("comment", ["", "-att-rev"], ids=["first-query", "later-query"])
def test_exit_one_cannot_claim_absence_when_listing_still_contains_owned_tag(
    tmp_path: Path, command: str, quoted: bool, comment: str
) -> None:
    state, log = _write_mock_iptables(tmp_path)
    unrelated = "FORWARD -s 10.8.0.1 -j DROP -m comment --comment astinuslab-isolate-job-abcd"
    before = _ordered_rules() + [unrelated]
    state.write_text("\n".join(before) + "\n")
    result = subprocess.run(
        ["bash", str(HELPER), command, "job-abc", "192.168.122.10", "10.0.0.5"],
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "IPTABLES_STATE": str(state),
            "IPTABLES_LOG": str(log),
            "IPTABLES_CHECK_EXIT": "1",
            "IPTABLES_CHECK_COMMENT": comment,
            "IPTABLES_QUOTE_COMMENTS": str(int(quoted)),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0, result.stderr
    expected = before if command == "apply" or not comment else before[:2] + [unrelated]
    assert state.read_text().splitlines() == expected
    mutations = [line for line in log.read_text().splitlines() if not line.startswith(("-C ", "-S "))]
    assert mutations == ([] if expected == before else [f"-D {_ordered_rules()[2]}"])


@pytest.mark.parametrize("quoted", [False, True], ids=["unquoted", "quoted"])
def test_absence_proof_distinguishes_exact_tags_from_other_jobs_and_exception_tags(
    tmp_path: Path, quoted: bool
) -> None:
    state, log = _write_mock_iptables(tmp_path)
    # The DROP is already absent. Each suffix must be checked independently;
    # neither another exception nor another Job with the same prefix blocks it.
    unrelated = "FORWARD -s 10.8.0.1 -j DROP -m comment --comment astinuslab-isolate-job-abcd"
    state.write_text("\n".join(_ordered_rules()[:2] + [unrelated]) + "\n")
    result = subprocess.run(
        ["bash", str(HELPER), "remove", "job-abc", "192.168.122.10", "10.0.0.5"],
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "IPTABLES_STATE": str(state),
            "IPTABLES_LOG": str(log),
            "IPTABLES_QUOTE_COMMENTS": str(int(quoted)),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert state.read_text().splitlines() == [unrelated]
    assert log.read_text().splitlines().count("-S FORWARD") == 3


@pytest.mark.parametrize("command", ["apply", "remove"])
def test_duplicate_rule_deletion_failure_is_retryable_and_preserves_unrelated_rules(
    tmp_path: Path, command: str
) -> None:
    state, log = _write_mock_iptables(tmp_path)
    unrelated = "FORWARD -s 10.8.0.1 -j DROP -m comment --comment another-owner"
    # Legacy runs can leave duplicate exceptions; keep only one deny because
    # apply does not need to rewrite an already present deny.
    before = _ordered_rules()[:2] * 2 + [_ordered_rules()[2], unrelated]
    state.write_text("\n".join(before) + "\n")
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "IPTABLES_STATE": str(state),
        "IPTABLES_LOG": str(log),
    }
    args = ["bash", str(HELPER), command, "job-abc", "192.168.122.10", "10.0.0.5"]
    result = subprocess.run(args, env={**env, "IPTABLES_DELETE_COMMENT": "-att-rev"}, capture_output=True)
    assert result.returncode != 0
    assert state.read_text().splitlines() == (before if command == "apply" else before[:4] + [unrelated])
    result = subprocess.run(args, env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert state.read_text().splitlines() == (_ordered_rules() + [unrelated] if command == "apply" else [unrelated])


@pytest.mark.parametrize("error,list_error", [(1, 1), (1, 0), (2, 0), (4, 0), (127, 0)])
def test_query_failure_retains_durable_firewall_custody_until_retry(
    tmp_path: Path, in_memory_session_scope: Any, error: int, list_error: int
) -> None:
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.models import CapacityReservation
    from astinuslab.db.repositories import JobRepository
    from astinuslab.jobs.capacity import confirm_cleanup
    from astinuslab.jobs.capacity_host import CleanupProof
    from astinuslab.workloads import workload_support

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        created_job = repo.create_job(
            JobCreate(target_id="fixture", scenario_id="manual", execution_mode="manual_trigger")
        )
        job_id = created_job.id
        repo.update_job(
            job_id, status="failed", network_isolated=True, guest_ip="192.168.122.10", attacker_ip="10.0.0.5"
        )
        repo.upsert_runtime(job_id, "target", f"astinuslab-{job_id}", state="destroyed")
        session.add(
            CapacityReservation(
                job_id=job_id,
                vm_count=1,
                memory_mb=2048,
                storage_bytes=1024**3,
                preserve_failed_vm=False,
                state="reserved",
                boot_started=True,
            )
        )
    state, log = _write_mock_iptables(tmp_path)
    before = [rule.replace("job-abc", job_id) for rule in _ordered_rules()]
    state.write_text("\n".join(before) + "\n")
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "IPTABLES_STATE": str(state),
        "IPTABLES_LOG": str(log),
        "IPTABLES_CHECK_EXIT": str(error),
        "IPTABLES_CHECK_COMMENT": "-att-rev",
        "IPTABLES_LIST_EXIT": str(list_error),
    }

    def run_fake_helper(command: list[str], **kwargs: Any) -> str:
        assert command[:2] == ["sudo", "-n"]
        assert command[3:] == ["remove", job_id, "192.168.122.10", "10.0.0.5"]
        assert "job_id" not in kwargs
        # Execute the source helper directly with fake iptables; never sudo.
        return subprocess.run(
            ["bash", str(HELPER), *command[3:]], env=env, capture_output=True, text=True, check=True
        ).stdout

    def remove_rules() -> bool:
        return workload_support.remove_network_isolation(
            in_memory_session_scope,
            JobRepository,
            run_fake_helper,
            logging.getLogger(__name__),
            job_id,
            "192.168.122.10",
            "10.0.0.5",
        )

    assert remove_rules() is False
    assert state.read_text().splitlines() == before[:2]
    proof = CleanupProof(job_id, True, True)
    with in_memory_session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        row = session.get(CapacityReservation, job_id)
        assert job is not None and row is not None
        assert job.network_isolated is True
        assert not confirm_cleanup(session, job_id, proof=proof)
        assert row.state == "reserved"
    del env["IPTABLES_CHECK_EXIT"]
    del env["IPTABLES_LIST_EXIT"]
    assert remove_rules() is True
    assert state.read_text() == ""
    with in_memory_session_scope() as session:
        job = JobRepository(session).get_job(job_id)
        row = session.get(CapacityReservation, job_id)
        assert job is not None and row is not None
        assert job.network_isolated is False
        assert confirm_cleanup(session, job_id, proof=proof)
        assert row.state == "released"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_helper_passes_shell_syntax_check():
    result = subprocess.run(
        ["bash", "-n", str(HELPER)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
