"""Rollback verifies saved service state without starting deliberately stopped units."""

import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INSTALL = ROOT / "ops/deploy/install.sh"
SPLIT = tuple(f"astinuslab-{role}.service" for role in ("api", "runner", "analysis"))
LEGACY = tuple(f"astinuslab-{role}@operator.service" for role in ("api", "runner", "analysis"))
pytestmark = [pytest.mark.unit, pytest.mark.host]


def rollback_probe(tmp_path, active, *, enabled=None, changes=None, static_failure="", malformed=None):
    """Keep the real rollback dispatcher/verifier; replace only host side effects."""
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    enabled = SPLIT if enabled is None else enabled
    for name, units in (("active", active), ("enabled", enabled)):
        if units:
            (snapshot / name).write_text("".join(unit + "\n" for unit in units))
    if malformed:
        (snapshot / malformed).write_text("foreign.service\n")
    state = {
        unit: {
            "active": "active" if unit in active else "inactive",
            "pid": "123" if unit in active else "0",
            "enabled": unit in enabled,
        }
        for unit in (*SPLIT, *LEGACY)
    }
    # Absent legacy templates model systemd's LoadState=not-found case: show
    # succeeds with ActiveState=inactive/MainPID=0; is-enabled is unsuccessful.
    # A failed D-Bus query is distinct and is modeled by query-error below.
    for unit, values in (changes or {}).items():
        state[unit].update(values)
    assignments = "\n".join(
        f"states[{shlex.quote(unit)}]={shlex.quote(str(values['active']))}; "
        f"pids[{shlex.quote(unit)}]={shlex.quote(str(values['pid']))}; "
        f"fixture_enabled[{shlex.quote(unit)}]={str(values['enabled']).lower()}"
        for unit, values in state.items()
    )
    calls = tmp_path / "calls"
    script = f"""
source {shlex.quote(str(INSTALL))}
MIGRATION_SNAPSHOT={shlex.quote(str(snapshot))}
PREVIOUS_RELEASE_DIR={shlex.quote(str(tmp_path))}
SERVICE_USER=operator
API_USER=astinuslab-api
RUNNER_USER=astinuslab-runner
ANALYSIS_USER=astinuslab-analysis
START_SERVICES=true
declare -A states=() pids=() fixture_enabled=()
{assignments}
fail_phase() {{ :; }}
quiesce_services() {{ :; }}
split_database_layout() {{ [[ "$1" == retain ]]; }}
release_rollback() {{ [[ "$2" == false ]]; }}
migration_rollback() {{ [[ "$1" == true ]]; }}
sleep() {{ :; }}
systemctl() {{
  printf '%s\n' "$*" >> {shlex.quote(str(calls))}
  case "$1" in
    is-active) [[ "${{states[$3]}}" == active ]] ;;
    is-enabled) [[ "${{fixture_enabled[$3]}}" == true ]] ;;
    show)
      [[ "${{states[$2]}}" != query-error ]] || return 1
      case "$4" in
        ActiveState) printf '%s\n' "${{states[$2]}}" ;;
        MainPID) printf '%s\n' "${{pids[$2]}}" ;;
        *) return 96 ;;
      esac ;;
    *) echo FORBIDDEN-SERVICE-MUTATION; return 97 ;;
  esac
}}
verify_runner_helpers() {{ echo static-helper-check; [[ {shlex.quote(static_failure or "none")} != helper ]]; }}
runuser() {{
  [[ "$*" == *'sudo -n -l'* ]] || return 98
  [[ {shlex.quote(static_failure or "none")} == sudo ]]
}}
id() {{
  if [[ "$1" == -nG ]]; then
    [[ {shlex.quote(static_failure or "none")} != group ]] || echo kvm
    echo astinuslab
  else
    [[ {shlex.quote(static_failure or "none")} != identity ]]
  fi
}}
run_deep_readiness_as() {{ echo DEEP-READINESS; }}
if rollback_existing_split_upgrade fixture; then echo RESULT=0; else echo RESULT=1; fi
"""
    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, cwd=ROOT, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FORBIDDEN" not in result.stdout
    operations = calls.read_text().splitlines() if calls.exists() else []
    assert all(line.split()[0] in {"is-active", "is-enabled", "show"} for line in operations)
    return result.stdout


@pytest.mark.parametrize(
    ("active", "enabled"),
    [((), ()), ((), (SPLIT[0],)), ((SPLIT[0],), (SPLIT[0],)), ((SPLIT[0], SPLIT[2]), (SPLIT[0],))],
)
def test_stopped_split_units_restore_without_full_runtime_claim(tmp_path, active, enabled):
    output = rollback_probe(tmp_path, active, enabled=enabled)
    assert "RESULT=0" in output
    assert "static-helper-check" in output
    assert "DEEP-READINESS" not in output
    assert "runtime readiness not asserted" in output
    assert "did not completely restore" not in output


def test_fully_active_split_rollback_keeps_readiness_with_absent_legacy_units(tmp_path):
    output = rollback_probe(tmp_path, SPLIT)
    assert "RESULT=0" in output
    assert "DEEP-READINESS" in output


@pytest.mark.parametrize(
    "changes",
    [
        {SPLIT[1]: {"active": "inactive", "pid": "0"}},
        {SPLIT[1]: {"enabled": False}},
        {LEGACY[0]: {"enabled": True}},
        {LEGACY[0]: {"active": "active", "pid": "321"}},
        {LEGACY[0]: {"active": "activating"}},
        {LEGACY[0]: {"active": "deactivating"}},
        {LEGACY[0]: {"pid": "321"}},
        {LEGACY[0]: {"active": "query-error"}},
    ],
)
def test_rollback_rejects_inexact_saved_state_without_service_mutation(tmp_path, changes):
    output = rollback_probe(tmp_path, SPLIT, changes=changes)
    assert "RESULT=1" in output
    assert "DEEP-READINESS" not in output


@pytest.mark.parametrize("failure", ["helper", "sudo", "identity", "group"])
def test_inactive_rollback_still_checks_static_identity_boundary(tmp_path, failure):
    output = rollback_probe(tmp_path, (), static_failure=failure)
    assert "static-helper-check" in output
    assert "RESULT=1" in output
    assert "DEEP-READINESS" not in output


@pytest.mark.parametrize("field", ["active", "enabled"])
def test_unknown_snapshot_unit_is_not_silently_ignored(tmp_path, field):
    output = rollback_probe(tmp_path, SPLIT, malformed=field)
    assert "RESULT=1" in output
    assert "DEEP-READINESS" not in output
