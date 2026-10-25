"""Contract for the atomic, mandatory service switch.

`systemctl start` is a no-op on an already-active unit, so an upgrade that used
it left the old processes executing a release the installer had just superseded.
The switch is now one ordered sequence — quiesce, swap `current`, reload, restart
every unit, verify — and every install takes the same path.
"""

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INSTALL = ROOT / "ops/deploy/install.sh"
pytestmark = [pytest.mark.unit, pytest.mark.host]


def run_bash(script: str, *, env=None, cwd=ROOT):
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(["bash", "-c", script], cwd=cwd, env=merged, capture_output=True, text=True)


def _recorded_systemctl(log: Path, *, active: tuple[str, ...] = ()) -> str:
    """A systemctl stand-in that records every verb it is asked to perform."""
    active_units = " ".join(active)
    return f"""
    systemctl() {{
      local verb="$1"; shift
      case "$verb" in
        is-active)
          local unit="${{*: -1}}"
          for candidate in {active_units or '""'}; do
            [[ "$unit" == "$candidate" ]] && return 0
          done
          return 1
          ;;
        is-enabled) return 1 ;;
        daemon-reload) echo "daemon-reload" >> {shlex.quote(str(log))} ;;
        *) echo "$verb ${{*: -1}}" >> {shlex.quote(str(log))} ;;
      esac
      return 0
    }}
    """


def _harness(install: Path, log: Path, body: str, *, active: tuple[str, ...] = ()) -> str:
    release = install / "releases/1.0.0-new000000000"
    return f"""
    source {INSTALL}
    INSTALL_DIR={shlex.quote(str(install))}
    RELEASE_DIR={shlex.quote(str(release))}
    STORAGE_ROOT={shlex.quote(str(install / "storage"))}
    SERVICE_USER=operator
    API_USER=operator
    RUNNER_USER=operator
    ANALYSIS_USER=operator
    SPLIT_USERS=false
    {_recorded_systemctl(log, active=active)}
    {body}
    """


def _verbs(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [line.split()[0] for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_control_services_never_starts_an_already_active_unit(tmp_path):
    """The defect this task exists to remove: `start` against a running unit
    leaves the old process attached to a superseded release."""
    install = tmp_path / "install"
    install.mkdir()
    log = tmp_path / "calls.log"
    active = tuple(f"astinuslab-{name}@operator.service" for name in ("api", "runner", "analysis"))

    result = run_bash(_harness(install, log, "control_services", active=active))

    assert result.returncode == 0, result.stderr
    verbs = _verbs(log)
    assert "start" not in verbs, f"an upgrade must never `systemctl start`: {verbs}"
    assert verbs.count("restart") == 3


def test_control_services_restarts_on_a_clean_install_too(tmp_path):
    """`restart` starts an inactive unit, so one verb covers both matrices and
    there is no second code path to get wrong."""
    install = tmp_path / "install"
    install.mkdir()
    log = tmp_path / "calls.log"

    result = run_bash(_harness(install, log, "control_services"))

    assert result.returncode == 0, result.stderr
    verbs = _verbs(log)
    assert "start" not in verbs, verbs
    assert verbs.count("restart") == 3


def test_no_start_enables_without_touching_the_running_services(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    log = tmp_path / "calls.log"

    result = run_bash(_harness(install, log, "START_SERVICES=false\n    control_services"))

    assert result.returncode == 0, result.stderr
    verbs = _verbs(log)
    assert "restart" not in verbs and "start" not in verbs, verbs
    assert verbs.count("enable") == 3


def test_the_switch_quiesces_swaps_reloads_then_restarts(tmp_path):
    """The whole point of the ordering: nothing is running while `current`
    moves, and the units are reloaded before anything is restarted."""
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("switch_release_and_restart()") : text.index("release_rollback()")]

    order = [
        body.index("quiesce_services"),
        body.index("switch_current_release"),
        body.index("daemon-reload"),
        body.index("control_services"),
    ]
    assert order == sorted(order), f"switch steps out of order: {order}"


def test_the_release_is_staged_but_not_switched_before_the_quiesce():
    """Staging copies bytes; switching redirects running services. Only the
    second belongs inside the stop/start window."""
    text = INSTALL.read_text(encoding="utf-8")
    stage = text[text.index("stage_release()") : text.index("remove_legacy_flat_source()")]

    assert "switch_current_release" not in stage, "staging must not move `current`"


def test_main_runs_the_switch_after_the_migration_and_verifies_it():
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("upgrade_single_identity_install()") : text.index("upgrade_existing_split_install()")]

    assert body.index("apply_db_migrations") < body.index("switch_release_and_restart")
    assert body.index("switch_release_and_restart") < body.index("verify_release_readiness")


def test_main_stays_failed_when_the_release_switch_rolls_back_successfully(tmp_path):
    """A successful rollback must not turn the failed switch into exit zero."""
    events = tmp_path / "events.log"
    log = shlex.quote(str(events))
    harness = f"""
    source {INSTALL}
    CHECK_ONLY=false
    SPLIT_USERS=false
    parse_args() {{ :; }}
    validate_existing_install_dir_state() {{ :; }}
    refuse_foreign_template_instances() {{ :; }}
    resolve_storage_root() {{ :; }}
    bind_storage_root_identity() {{ :; }}
    assert_storage_root_identity() {{ :; }}
    validate_install_storage_root() {{ :; }}
    require_root() {{ :; }}
    validate_existing_install_dir_authority() {{ :; }}
    verify_storage_root_matches_config() {{ :; }}
    check_prerequisites() {{ :; }}
    ensure_service_user() {{ :; }}
    install_source() {{ :; }}
    create_venv() {{ :; }}
    migration_snapshot_mutable_state() {{ :; }}
    prepare_single_upgrade_mutable_state() {{ :; }}
    prepare_storage() {{ :; }}
    quiesce_services() {{ :; }}
    backup_database_before_migration() {{ :; }}
    apply_db_migrations() {{ :; }}
    handle_ssh_key() {{ :; }}
    install_sudoers_rule() {{ :; }}
    configure_mac() {{ :; }}
    install_systemd_units() {{ :; }}
    migration_snapshot() {{ :; }}
    migration_rollback() {{ :; }}
    migration_discard_snapshot() {{ :; }}
    switch_release_and_restart() {{ echo switch >> {log}; return 1; }}
    release_rollback() {{ echo rollback >> {log}; return 0; }}
    verify_release_readiness() {{ echo FORBIDDEN-readiness >> {log}; }}
    print_success() {{ echo FORBIDDEN-success >> {log}; }}
    die() {{ echo die >> {log}; exit 7; }}
    main
    """

    result = run_bash(harness)

    assert result.returncode == 7, result.stdout + result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == ["switch", "rollback", "die"]


def test_rollback_restores_the_database_before_it_restarts_anything(tmp_path):
    install = tmp_path / "install"
    previous = install / "releases/1.0.0-previous0000"
    current = install / "releases/1.0.0-new000000000"
    previous.mkdir(parents=True)
    current.mkdir(parents=True)
    (install / "current").symlink_to(current)
    order = tmp_path / "order.log"
    log = tmp_path / "calls.log"

    body = f"""
    PREVIOUS_RELEASE_DIR={shlex.quote(str(previous))}
    DB_BACKUP={shlex.quote(str(tmp_path / "pre.db"))}
    quiesce_services() {{ echo quiesce >> {shlex.quote(str(order))}; }}
    restore_database_from_backup() {{ echo restore >> {shlex.quote(str(order))}; }}
    control_services() {{ echo restart >> {shlex.quote(str(order))}; }}
    verify_release_readiness() {{ echo verify >> {shlex.quote(str(order))}; }}
    release_rollback "readiness failed"
    """
    result = run_bash(_harness(install, log, body))

    assert result.returncode == 0, result.stderr
    assert order.read_text(encoding="utf-8").split() == ["quiesce", "restore", "restart", "verify"]
    assert os.readlink(install / "current").endswith("1.0.0-previous0000")


def test_no_start_is_recorded_as_leaving_the_switch_unverified():
    """Deep readiness cannot pass against services that were never started, so
    --no-start has to say so rather than silently skipping the gate."""
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("verify_release_readiness()") : text.index("verify_split_services()")]

    assert re.search(r"START_SERVICES.*\|\|.*return 0|\$START_SERVICES \|\| \{", body), body
    assert "warn" in body, "skipping the verification has to be visible"


def test_existing_split_upgrade_reuses_the_atomic_release_path(tmp_path):
    events = tmp_path / "events.log"
    log = shlex.quote(str(events))
    harness = f"""
    source {INSTALL}
    SPLIT_USERS=true
    SERVICE_USER=operator
    API_USER=astinuslab-api
    RUNNER_USER=astinuslab-runner
    ANALYSIS_USER=astinuslab-analysis
    install_split_upgrade_source() {{ echo stage >> {log}; }}
    create_venv() {{ echo FORBIDDEN-venv >> {log}; }}
    prepare_split_upgrade_mutable_state() {{ echo exact-permissions >> {log}; }}
    quiesce_services() {{ echo quiesce >> {log}; }}
    migration_snapshot_mutable_state() {{ echo mutable-snapshot >> {log}; }}
    backup_database_before_migration() {{ echo backup >> {log}; }}
    apply_db_migrations() {{ echo migrate >> {log}; }}
    handle_ssh_key() {{ echo key >> {log}; }}
    resolve_storage_root() {{ echo root >> {log}; }}
    bind_storage_root_identity() {{ :; }}
    assert_storage_root_identity() {{ :; }}
    configure_mac() {{ echo mac >> {log}; }}
    install_systemd_units() {{ echo units >> {log}; }}
    finalize_split_sudoers() {{ echo sudoers >> {log}; }}
    verify_final_runner_helpers() {{ echo helpers >> {log}; }}
    switch_release_and_restart() {{ echo switch >> {log}; }}
    verify_split_services() {{ echo readiness >> {log}; }}
    migration_snapshot() {{ echo snapshot >> {log}; }}
    migration_commit() {{ echo commit >> {log}; }}
    cleanup_superseded_legacy_install() {{ :; }}
    migrate_to_split_users() {{ echo FORBIDDEN-identities >> {log}; }}
    upgrade_existing_split_install
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == [
        "snapshot",
        "stage",
        "quiesce",
        "mutable-snapshot",
        "backup",
        "root",
        "exact-permissions",
        "migrate",
        "key",
        "mac",
        "units",
        "sudoers",
        "helpers",
        "switch",
        "readiness",
        "commit",
    ]


def test_existing_split_upgrade_rolls_back_and_stays_failed_after_migration_error(tmp_path):
    events = tmp_path / "events.log"
    log = shlex.quote(str(events))
    harness = f"""
    source {INSTALL}
    SPLIT_USERS=true
    install_split_upgrade_source() {{ echo stage >> {log}; }}
    create_venv() {{ echo FORBIDDEN-venv >> {log}; }}
    quiesce_services() {{ echo quiesce >> {log}; }}
    migration_snapshot_mutable_state() {{ echo mutable-snapshot >> {log}; }}
    backup_database_before_migration() {{ echo backup >> {log}; }}
    resolve_storage_root() {{ :; }}
    bind_storage_root_identity() {{ :; }}
    prepare_split_upgrade_mutable_state() {{ :; }}
    apply_db_migrations() {{ echo migrate >> {log}; return 1; }}
    split_database_layout() {{ [[ "$1" == retain ]] || return 98; echo retain >> {log}; }}
    release_rollback() {{ echo rollback >> {log}; return 0; }}
    migration_snapshot() {{ echo snapshot >> {log}; return 0; }}
    migration_rollback() {{ echo privileged-rollback >> {log}; return 0; }}
    verify_split_services() {{ echo readiness >> {log}; return 0; }}
    switch_release_and_restart() {{ echo FORBIDDEN-switch >> {log}; }}
    if upgrade_existing_split_install; then exit 99; fi
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == [
        "snapshot",
        "stage",
        "quiesce",
        "mutable-snapshot",
        "backup",
        "migrate",
        "quiesce",
        "retain",
        "rollback",
        "privileged-rollback",
        "readiness",
    ]


def test_split_rollback_quiesces_before_database_restore(tmp_path):
    events = tmp_path / "events.log"
    log = shlex.quote(str(events))
    result = run_bash(f"""
    source {INSTALL}
    quiesce_services() {{ echo quiesce >> {log}; }}
    split_database_layout() {{ [[ "$1" == retain ]] || return 98; echo retain >> {log}; }}
    release_rollback() {{ echo release-rollback >> {log}; }}
    migration_rollback() {{ echo privileged-rollback >> {log}; }}
    verify_split_services() {{ echo verify >> {log}; }}
    rollback_existing_split_upgrade failed
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == [
        "quiesce",
        "retain",
        "release-rollback",
        "privileged-rollback",
        "verify",
    ]


def test_split_rollback_verifies_the_restored_release_not_the_candidate(tmp_path):
    previous = tmp_path / "previous"
    previous.mkdir()
    observed = tmp_path / "verified-release"
    result = run_bash(f"""
    source {INSTALL}
    PREVIOUS_RELEASE_DIR={shlex.quote(str(previous))}
    RELEASE_DIR=/opt/astinuslab/releases/candidate
    quiesce_services() {{ :; }}
    split_database_layout() {{ [[ "$1" == retain ]]; }}
    release_rollback() {{ :; }}
    migration_rollback() {{ :; }}
    verify_split_services() {{ printf '%s\n' "$1" > {shlex.quote(str(observed))}; }}
    rollback_existing_split_upgrade failed
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert observed.read_text(encoding="utf-8").strip() == str(previous)


def test_single_upgrade_failure_restores_release_then_privileged_snapshot(tmp_path):
    events = tmp_path / "events.log"
    previous = tmp_path / "previous"
    previous.mkdir()
    log = shlex.quote(str(events))
    result = run_bash(f"""
    source {INSTALL}
    PREVIOUS_RELEASE_DIR={shlex.quote(str(previous))}
    release_rollback() {{ echo release-rollback >> {log}; }}
    migration_rollback() {{ echo privileged-rollback >> {log}; }}
    verify_release_readiness() {{ echo verify >> {log}; }}
    rollback_existing_single_upgrade failed
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == [
        "release-rollback",
        "privileged-rollback",
        "verify",
    ]


@pytest.mark.parametrize(
    ("wrapper", "readiness"),
    [
        ("rollback_existing_single_upgrade", "verify_release_readiness"),
        ("rollback_existing_split_upgrade", "verify_split_services"),
    ],
)
def test_release_restore_failure_keeps_privileged_rollback_runtime_quiesced(tmp_path, wrapper, readiness):
    events = tmp_path / "events.log"
    log = shlex.quote(str(events))
    result = run_bash(f"""
    source {INSTALL}
    quiesce_services() {{ :; }}
    split_database_layout() {{ [[ "$1" == retain ]]; }}
    release_rollback() {{ echo release-failed >> {log}; return 1; }}
    migration_rollback() {{ echo "privileged-restart=$1" >> {log}; return 0; }}
    {readiness}() {{ echo FORBIDDEN-readiness >> {log}; return 0; }}
    if {wrapper} "database restore failed"; then exit 99; fi
    """)

    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == [
        "release-failed",
        "privileged-restart=false",
    ]


def test_privileged_rollback_disables_candidate_units_before_restoring_snapshot():
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("migration_rollback()") : text.index("migration_commit()")]

    for unit in (
        '"astinuslab-api@${SERVICE_USER}.service"',
        '"astinuslab-runner@${SERVICE_USER}.service"',
        '"astinuslab-analysis@${SERVICE_USER}.service"',
        "astinuslab-api.service",
        "astinuslab-runner.service",
        "astinuslab-analysis.service",
    ):
        assert unit in body
    assert 'systemctl disable "$unit"' in body


@pytest.mark.parametrize(
    ("failing_step", "expected_tail"),
    [
        ("switch", ["switch", "rollback"]),
        ("readiness", ["switch", "readiness", "rollback"]),
    ],
)
def test_existing_split_upgrade_rolls_back_and_stays_failed_after_runtime_error(tmp_path, failing_step, expected_tail):
    events = tmp_path / "events.log"
    log = shlex.quote(str(events))
    switch_result = "return 1" if failing_step == "switch" else "return 0"
    harness = f"""
    source {INSTALL}
    SPLIT_USERS=true
    install_split_upgrade_source() {{ :; }}
    create_venv() {{ :; }}
    quiesce_services() {{ :; }}
    migration_snapshot_mutable_state() {{ :; }}
    backup_database_before_migration() {{ :; }}
    prepare_split_upgrade_mutable_state() {{ :; }}
    apply_db_migrations() {{ :; }}
    handle_ssh_key() {{ :; }}
    resolve_storage_root() {{ :; }}
    bind_storage_root_identity() {{ :; }}
    assert_storage_root_identity() {{ :; }}
    configure_mac() {{ :; }}
    install_systemd_units() {{ :; }}
    finalize_split_sudoers() {{ :; }}
    verify_final_runner_helpers() {{ :; }}
    switch_release_and_restart() {{ echo switch >> {log}; {switch_result}; }}
    readiness_calls=0
    verify_split_services() {{
      echo readiness >> {log}
      readiness_calls=$((readiness_calls + 1))
      if [[ {shlex.quote(failing_step)} == readiness && "$readiness_calls" == 1 ]]; then return 1; fi
      return 0
    }}
    release_rollback() {{ echo rollback >> {log}; return 0; }}
    migration_snapshot() {{ echo snapshot >> {log}; return 0; }}
    migration_rollback() {{ echo privileged-rollback >> {log}; return 0; }}
    migration_commit() {{ echo FORBIDDEN-commit >> {log}; }}
    if upgrade_existing_split_install; then exit 99; fi
    """

    result = run_bash(harness)

    assert result.returncode == 0, result.stdout + result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == [
        "snapshot",
        *expected_tail,
        "privileged-rollback",
        "readiness",
    ]


def test_main_routes_a_valid_existing_split_install_around_identity_migration():
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("main() {") :]

    assert "split_identity_marker_is_valid" in body
    assert "upgrade_existing_split_install" in body
    assert body.index("upgrade_existing_split_install") < body.index("bootstrap_split_candidate_if_missing")


def test_release_readiness_uses_the_api_identity_after_a_split():
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("verify_release_readiness()") : text.index("verify_split_services()")]

    assert 'readiness_user="$SERVICE_USER"' in body
    assert '$SPLIT_USERS && readiness_user="$API_USER"' in body
    assert 'run_deep_readiness_as "$readiness_user"' in body
