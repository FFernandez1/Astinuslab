"""Contract for the one storage root.

Policy, systemd units, the sudoers rule, the AppArmor rule and the application
must all name the same storage root. Deep readiness compares the *application's*
configured storage against the installed policy and fails the switch when they
disagree - after the services have already been stopped. Registering a custom
root with `configure-privileged-roots.sh` and never configuring the application
for it therefore took the next upgrade down and left the host not serving, which
is worse than refusing before anything moves.
"""

import os
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


def _install_root(tmp_path: Path, *, storage_dir: str | None, env_override: str | None = None) -> Path:
    install = tmp_path / "install"
    (install / "config").mkdir(parents=True)
    body = "[storage]\n"
    if storage_dir is not None:
        body += f'storage_dir = "{storage_dir}"\n'
    (install / "astinuslab.toml").write_text(body, encoding="utf-8")
    if env_override is not None:
        (install / "config" / "astinuslab.env").write_text(
            f"ASTINUSLAB_API_PORT=8000\nASTINUSLAB_STORAGE_DIR={env_override}\n", encoding="utf-8"
        )
    return install


def _check(install: Path, storage_root: str):
    return run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        STORAGE_ROOT={shlex.quote(storage_root)}
        if verify_storage_root_matches_config; then echo "accepted"; else echo "refused"; fi
    """)


def test_a_privileged_root_the_application_does_not_use_is_refused(tmp_path):
    install = _install_root(tmp_path, storage_dir="storage")

    result = _check(install, "/srv/astinuslab-evidence")

    combined = result.stdout + result.stderr
    assert "accepted" not in combined
    assert "/srv/astinuslab-evidence" in combined, "the refusal must name the privileged root"
    assert str(install / "storage") in combined, "and the root the application actually uses"


def test_a_matching_absolute_root_is_accepted(tmp_path):
    install = _install_root(tmp_path, storage_dir="/srv/astinuslab-evidence")

    result = _check(install, "/srv/astinuslab-evidence")

    assert "accepted" in result.stdout, result.stdout + result.stderr


def test_a_relative_storage_dir_resolves_against_the_install_root(tmp_path):
    install = _install_root(tmp_path, storage_dir="storage")

    result = _check(install, str(install / "storage"))

    assert "accepted" in result.stdout, result.stdout + result.stderr


def test_a_base_dir_template_is_resolved_like_the_application_resolves_it(tmp_path):
    install = _install_root(tmp_path, storage_dir="{base_dir}/storage")

    result = _check(install, str(install / "storage"))

    assert "accepted" in result.stdout, result.stdout + result.stderr


def test_an_environment_override_is_what_the_units_will_actually_use(tmp_path):
    """`config/astinuslab.env` is read by every unit through EnvironmentFile and
    overrides the TOML, so it is the value that decides where evidence lands."""
    install = _install_root(tmp_path, storage_dir="storage", env_override="/srv/from-env")

    result = _check(install, str(install / "storage"))

    combined = result.stdout + result.stderr
    assert "accepted" not in combined
    assert "/srv/from-env" in combined


def test_an_install_with_no_configuration_yet_is_not_second_guessed(tmp_path):
    """A clean install has no astinuslab.toml until this run seeds it."""
    install = tmp_path / "install"
    install.mkdir()

    result = _check(install, str(install / "storage"))

    assert "accepted" in result.stdout, result.stdout + result.stderr


def test_malformed_existing_configuration_is_refused_before_mutation(tmp_path):
    install = tmp_path / "install"
    (install / "config").mkdir(parents=True)
    (install / "astinuslab.toml").write_text("[storage\nstorage_dir = ???\n", encoding="utf-8")

    result = _check(install, str(install / "storage"))

    combined = result.stdout + result.stderr
    assert "accepted" not in combined
    assert "could not determine configured storage root" in combined


@pytest.mark.parametrize("relative", ["astinuslab.toml", "config/astinuslab.env"])
@pytest.mark.parametrize("kind", ["directory", "broken-symlink"])
def test_existing_non_regular_configuration_is_refused(tmp_path, relative, kind):
    install = tmp_path / "install"
    path = install / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(tmp_path / "missing-target")

    result = _check(install, str(install / "storage"))

    combined = result.stdout + result.stderr
    assert "accepted" not in combined
    assert "could not determine configured storage root" in combined


def test_an_unreadable_existing_environment_file_is_refused(tmp_path):
    install = _install_root(tmp_path, storage_dir="storage")
    env_file = install / "config" / "astinuslab.env"
    env_file.write_text("ASTINUSLAB_STORAGE_DIR=/srv/hidden\n", encoding="utf-8")
    env_file.chmod(0)
    try:
        result = _check(install, str(install / "storage"))
    finally:
        env_file.chmod(0o600)

    combined = result.stdout + result.stderr
    assert "accepted" not in combined
    assert "could not determine configured storage root" in combined


def test_a_storage_parser_process_failure_is_refused(tmp_path):
    install = _install_root(tmp_path, storage_dir="storage")
    result = run_bash(f"""
        source {INSTALL}
        INSTALL_DIR={shlex.quote(str(install))}
        STORAGE_ROOT={shlex.quote(str(install / "storage"))}
        configured_storage_root() {{ return 23; }}
        if verify_storage_root_matches_config; then echo accepted; else echo refused; fi
    """)

    combined = result.stdout + result.stderr
    assert "accepted" not in combined
    assert "could not determine configured storage root" in combined


def test_the_check_runs_before_the_installer_changes_anything():
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("main() {") :]

    assert body.index("verify_storage_root_matches_config") < body.index("upgrade_single_identity_install")
    upgrade = text[text.index("upgrade_single_identity_install()") : text.index("upgrade_existing_split_install()")]
    assert "install_source" in upgrade
    assert upgrade.index("install_source") < upgrade.index("quiesce_services")


def test_deep_readiness_loads_the_same_environment_file_as_systemd(tmp_path):
    install = tmp_path / "install"
    release = tmp_path / "release"
    env_file = install / "config" / "astinuslab.env"
    probe = release / ".venv" / "bin" / "python"
    observed = tmp_path / "observed"
    env_file.parent.mkdir(parents=True)
    probe.parent.mkdir(parents=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_timeout = fake_bin / "timeout"
    timeout_marker = tmp_path / "fake-timeout-ran"
    fake_timeout.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(timeout_marker))}\nexit 99\n",
        encoding="utf-8",
    )
    fake_timeout.chmod(0o755)
    env_file.write_text(
        f'; systemd-style comment\nASTINUSLAB_STORAGE_DIR="/srv/evidence # root"\nPATH={fake_bin}\n',
        encoding="utf-8",
    )
    probe.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n%s\\n\' "$ASTINUSLAB_STORAGE_DIR" "${{ASTINUSLAB_DB_PATH-unset}}" '
        f"> {shlex.quote(str(observed))}\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)
    result = run_bash(
        f"""
            source {INSTALL}
            INSTALL_DIR={shlex.quote(str(install))}
            runuser() {{ shift 3; "$@"; }}
            run_deep_readiness_as operator {shlex.quote(str(release))} 'raise SystemExit(0)'
        """,
        env={"ASTINUSLAB_DB_PATH": "/ambient/wrong.db"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert observed.read_text(encoding="utf-8").splitlines() == ["/srv/evidence # root", "unset"]
    assert not timeout_marker.exists()
