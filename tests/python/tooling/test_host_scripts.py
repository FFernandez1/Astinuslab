import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
# Tracked shell scripts deliberately outside the CI lint gate. Anything not
# matched by the CI globs must appear here or the coverage test fails.
INTENTIONALLY_UNLINTED: tuple[str, ...] = ()
pytestmark = [pytest.mark.unit, pytest.mark.host]


def _ci_bash_array(name: str) -> list[str]:
    """Read a bash array literal out of the shell-boundary CI job.

    The array is repeated once per gate step (steps are separate shells), so
    every occurrence must be identical or the two gates would drift apart.
    """
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    matches = re.findall(rf"^\s*{re.escape(name)}=\(\n(.*?)\n\s*\)\s*$", workflow, re.MULTILINE | re.DOTALL)
    assert matches, f"{name} array not found in {CI_WORKFLOW}"
    entries = [[line.strip() for line in match.splitlines() if line.strip()] for match in matches]
    assert all(entry == entries[0] for entry in entries), f"CI copies of {name} have drifted apart: {entries}"
    return entries[0]


def _repo_shell_scripts() -> list[Path]:
    """Every script the CI lint globs actually select.

    Derived from the workflow itself so these tests lint exactly what CI lints;
    coverage against the real tree is asserted separately by
    `test_ci_lint_globs_cover_every_tracked_shell_script`, which enumerates the
    tree independently instead of re-deriving it from the same list.
    """
    patterns = _ci_bash_array("SHELL_LINT_SCRIPTS")
    return sorted({ROOT / match for pattern in patterns for match in ROOT.glob(pattern)})


def _tracked_shell_scripts() -> list[Path]:
    """Every tracked *.sh in the repo, straight from git — not from any list here."""
    result = subprocess.run(["git", "ls-files", "-z", "*.sh"], cwd=ROOT, capture_output=True, text=True, check=True)
    return sorted(ROOT / entry for entry in result.stdout.split("\0") if entry)


def test_ci_lints_shell_scripts_by_glob_not_by_hardcoded_file_list():
    patterns = _ci_bash_array("SHELL_LINT_SCRIPTS")

    assert patterns, "CI must declare shell lint globs"
    assert all("*" in pattern and pattern.endswith(".sh") for pattern in patterns), (
        f"CI must lint by glob so new scripts are covered automatically, got: {patterns}"
    )


def test_ci_lint_globs_cover_every_tracked_shell_script():
    """The coverage claim, checked against the tree rather than against itself.

    An earlier version of this test built its expectation from the same four
    directories the CI globs named, so `missing` was structurally always empty
    and 17 tracked scripts — including the launcher and the image downloader the
    installer tells operators to run — were silently unlinted while the gate
    claimed to cover everything.
    """
    linted = set(_repo_shell_scripts())
    exempt = {ROOT / entry for entry in INTENTIONALLY_UNLINTED}

    stale = [entry for entry in INTENTIONALLY_UNLINTED if not (ROOT / entry).is_file()]
    assert not stale, f"INTENTIONALLY_UNLINTED names paths that no longer exist: {stale}"

    uncovered = [
        str(path.relative_to(ROOT)) for path in _tracked_shell_scripts() if path not in linted and path not in exempt
    ]
    assert not uncovered, (
        f"tracked shell scripts are neither CI-linted nor listed in INTENTIONALLY_UNLINTED: {uncovered}"
    )


def _ci_step_body(step_name: str) -> str:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(rf"- name: {re.escape(step_name)}\n(.*?)(?=\n\s*- name: )", workflow, re.DOTALL)
    assert match, f"CI step not found: {step_name}"
    return match.group(1)


def test_ci_syntax_gate_invokes_bash_n_one_file_at_a_time():
    """`bash -n a.sh b.sh` parses ONLY the first operand.

    The rest become positional parameters and are never checked. The gate once
    read `bash -n install.sh uninstall.sh ...` and silently checked one of five
    files. The test that was supposed to guard it only asserted that each script
    name appeared somewhere in the command, which the buggy single-invocation
    form satisfies — so this asserts the shape of the invocation instead.
    """
    body = _ci_step_body("Check shell syntax")

    commands = [part.strip() for chunk in body.splitlines() for part in chunk.split(";")]
    multi_operand = [command for command in commands if re.match(r"^bash\s+-n\s+\S+\.sh\s+\S+\.sh", command)]
    assert not multi_operand, f"bash -n must take one operand per invocation, got: {multi_operand}"

    assert re.search(r"for\s+\w+\s+in\s+\"\$\{SHELL_LINT_SCRIPTS\[@\]\}\"", body), (
        "CI must sweep every globbed script, not just the pinned five"
    )
    assert re.search(r"bash\s+-n\s+\"\$\w+\"", body), "the sweep must invoke bash -n with a single quoted operand"


def test_ci_declares_no_shellcheck_exemption_list():
    """Every globbed script is ShellCheck-clean, so no exemption array exists.

    The list this replaces held nine guest-side scripts carrying twenty findings
    between them; all nine were fixed rather than exempted. Reintroducing an
    array here would let a failing script back into the tree by path, which is
    exactly the drift the shell gates exist to prevent. A script that genuinely
    cannot pass takes a narrow in-file ``# shellcheck disable=`` with a reason,
    where the reviewer of that script can see it.
    """
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "SHELLCHECK_LEGACY_EXEMPT" not in workflow, (
        "the ShellCheck exemption array is back in ci.yml; fix or annotate the script instead"
    )
    assert not re.search(r"^\s*SHELLCHECK\w*EXEMPT\w*=\(", workflow, re.MULTILINE), (
        "ci.yml declares a ShellCheck exemption array under a new name"
    )


def test_linted_shell_scripts_pass_shellcheck():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")

    scripts = _repo_shell_scripts()
    assert scripts
    assert any(path.parent == ROOT / "ops/host" for path in scripts)

    result = subprocess.run(
        [shellcheck, "-f", "gcc", *(str(script) for script in scripts)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_linted_shell_scripts_have_valid_bash_syntax():
    # `bash -n a.sh b.sh` parses only the first operand, so check one per call.
    failures = []
    for script in _repo_shell_scripts():
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        if result.returncode != 0:
            failures.append(f"{script.relative_to(ROOT)}: {result.stderr.strip()}")

    assert not failures, "\n".join(failures)


def test_libvirt_storage_admin_has_valid_shell_syntax():
    script = ROOT / "ops/host/libvirt_storage_admin.sh"

    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_astinuslab_launcher_has_valid_shell_syntax():
    script = ROOT / "astinuslab.sh"

    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_install_sudoers_script_has_valid_shell_syntax():
    script = ROOT / "ops/host/install_sudoers_astinuslab.sh"

    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_install_sudoers_script_source_is_inert_and_defines_render_helper():
    script = ROOT / "ops/host/install_sudoers_astinuslab.sh"

    result = subprocess.run(
        ["bash", "-c", f"source {script}; type -t render_helper_template; type -t parse_args"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["function", "function"]


def test_install_sudoers_render_helper_preserves_sed_sensitive_path(tmp_path):
    script = ROOT / "ops/host/install_sudoers_astinuslab.sh"
    src = tmp_path / "helper.sh"
    dst = tmp_path / "rendered.sh"
    install_dir = "/tmp/astinuslab-test-a&b|c\\d"
    src.write_text("BASE=@@INSTALL_DIR@@\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", "-c", f"source {script}; render_helper_template {src} {dst} '{install_dir}'"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert dst.read_text(encoding="utf-8") == f"BASE={install_dir}\n"


def test_install_sudoers_script_accepts_explicit_run_user_and_source_dir():
    script = (ROOT / "ops/host/install_sudoers_astinuslab.sh").read_text(encoding="utf-8")

    assert "--run-user USER" in script
    assert "--helper-source-dir PATH" in script
    assert 'RUN_USER="$2"' in script
    assert 'HELPER_SOURCE_DIR="$2"' in script


def test_astinuslab_launcher_guards_against_mixed_runner_modes():
    script = (ROOT / "astinuslab.sh").read_text(encoding="utf-8")

    assert 'SYSTEMD_API_UNIT="astinuslab-api@${SERVICE_USER}.service"' in script
    assert 'SYSTEMD_RUNNER_UNIT="astinuslab-runner@${SERVICE_USER}.service"' in script
    assert "_active_systemd_units" in script
    assert "Manual start refused" in script
    assert "Active runner mode:" in script
    assert "mixed/conflict" in script


def test_libvirt_storage_admin_hardens_allowed_source_prefix():
    implementation = (ROOT / "ops/host/libvirt_storage_admin.py").read_text()

    assert "_parts_beneath(allowed_prefix, install_root" in implementation
    assert "source is outside allowed source prefix" in implementation
    assert "expected_identity=install_identity" in implementation


def test_libvirt_storage_admin_uses_descriptor_safe_filesystem_helper():
    wrapper = (ROOT / "ops/host/libvirt_storage_admin.sh").read_text()
    implementation = ROOT / "ops/host/libvirt_storage_admin.py"

    assert implementation.is_file()
    assert 'exec python3 -B "$SCRIPT_DIR/libvirt_storage_admin.py"' in wrapper
    assert "os.O_NOFOLLOW" in implementation.read_text()
    for unsafe in ("realpath -m", "cp -f", "rm -rf", 'chmod 0644 "$'):
        assert unsafe not in wrapper


def test_build_vol3_symbol_fails_fast_on_unsupported_package_managers():
    script = (ROOT / "guest/scripts/build_vol3_symbol.sh").read_text()

    assert "command -v zypper" in script
    assert "command -v pacman" in script
    assert "command -v apk" in script
    # zypper and pacman have real implementations; they no longer bail with exit 2.
    assert "_install_debug_pkg_zypper" in script
    assert "_install_debug_pkg_pacman" in script


def test_build_vol3_symbol_zypper_branch_attempts_kernel_default_debuginfo():
    script = (ROOT / "guest/scripts/build_vol3_symbol.sh").read_text()
    assert "_install_debug_pkg_zypper" in script
    assert "kernel-default-debuginfo" in script


def test_build_vol3_symbol_pacman_branch_uses_debuginfod_find():
    script = (ROOT / "guest/scripts/build_vol3_symbol.sh").read_text()
    assert "_install_debug_pkg_pacman" in script
    assert "debuginfod-find" in script
    # Vmlinux must land where _find_vmlinux looks.
    assert "/usr/lib/debug/boot/vmlinux-${KERNEL_RELEASE}" in script


def test_publish_disk_to_host_verifies_final_path_under_publish_dir():
    script = (ROOT / "ops/host/publish_disk_to_host.sh").read_text()

    assert 'CANONICAL_PUBLISH_DIR="$(realpath -m "$PUBLISH_DIR")"' in script
    assert 'CANONICAL_FINAL_PATH="$(realpath -m "$FINAL_PATH")"' in script
    assert (
        '[[ "$CANONICAL_FINAL_PATH" != "$CANONICAL_PUBLISH_DIR"/* '
        '&& "$CANONICAL_FINAL_PATH" != "$CANONICAL_PUBLISH_DIR" ]]'
    ) in script


# Keyed lines a compromised guest tries to smuggle out through a setup step
# (mkdir/upload/chmod). Both key namespaces are covered: the ones this script
# emits and the ones the orchestrator parses out of the merged job log
# (src/astinuslab/forensics/vol3_support.py::_parse_keyed_output).
FORGED_SETUP_LINES = (
    "vol3_symbol_path=/tmp/forged-setup.json",
    "isf_path=/tmp/forged-setup.json",
    "kernel_release=forged-setup",
    "build_id=abcdef01",
)


def _remote_handoff_harness(tmp_path):
    script = ROOT / "ops/host/build_vol3_symbol_remote.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    network_log = tmp_path / "network.jsonl"
    expansion_marker = tmp_path / "LOCAL_EXPANSION_OCCURRED"
    remote_tmp = f"/tmp/vol3 dir;$(touch {expansion_marker});`touch {expansion_marker}`;$HOME&'\"[]"
    kernel_release = "6.8.0-test"
    remote_dwarf = f"{remote_tmp}/dwarf2json"
    remote_symbol = f"{remote_tmp}/linux-{kernel_release}.json"

    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

def forge_setup_output():
    # A hostile guest login shell writing keyed lines during a setup step.
    if os.environ.get("SETUP_FORGED_OUTPUT") != "1":
        return
    for line in os.environ["FORGED_SETUP_LINES"].split("\\n"):
        print(line)
    print("forged-setup-on-guest-stderr", file=sys.stderr)


with open(os.environ["NETWORK_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps({"tool": "ssh", "args": sys.argv[1:]}) + "\\n")
command = sys.argv[-1]
stage = os.environ.get("FAIL_STAGE", "")
if command.startswith("mkdir "):
    forge_setup_output()
    raise SystemExit(70 if stage == "mkdir" else 0)
if command.startswith("sha256sum "):
    if stage == "guest_hash":
        raise SystemExit(71)
    print("Warning: simulated SSH host-key notice", file=sys.stderr)
    print(f"{os.environ['GUEST_HASH']}  {os.environ['REMOTE_DWARF']}")
elif command.startswith("chmod "):
    forge_setup_output()
    raise SystemExit(72 if stage == "chmod" else 0)
elif "bash -s --" in command:
    if stage == "build":
        raise SystemExit(73)
    if stage != "invalid_output":
        print(f"vol3_symbol_remote_path={os.environ['REMOTE_SYMBOL']}")
        print(f"kernel_release={os.environ['KERNEL_RELEASE']}")
        if os.environ.get("OMIT_GUEST_BUILD_ID") != "1":
            print("build_id=deadbeef")
elif command.startswith("rm -rf ") and os.environ.get("CLEANUP_FORGED_OUTPUT") == "1":
    print("vol3_symbol_path=/tmp/forged-cleanup.json")
    print("kernel_release=forged-cleanup")
    print("forged-cleanup-stderr", file=sys.stderr)
""",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)

    fake_scp = fake_bin / "scp"
    fake_scp.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

with open(os.environ["NETWORK_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps({"tool": "scp", "args": sys.argv[1:]}) + "\\n")
stage = os.environ.get("FAIL_STAGE", "")
is_upload = ":" in sys.argv[-1]
if is_upload:
    if sys.argv[-2].startswith("-"):
        raise SystemExit(76)
    if os.environ.get("SETUP_FORGED_OUTPUT") == "1":
        # A hostile guest sshd banner writing keyed lines during the upload.
        for line in os.environ["FORGED_SETUP_LINES"].split("\\n"):
            print(line)
        print("forged-setup-on-guest-stderr", file=sys.stderr)
    raise SystemExit(74 if stage == "upload" else 0)
if stage == "download":
    raise SystemExit(75)
Path(sys.argv[-1]).write_text("" if stage == "empty" else "{}\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    fake_scp.chmod(0o755)

    fake_mktemp = fake_bin / "mktemp"
    fake_mktemp.write_text(
        """#!/usr/bin/env python3
import os
from pathlib import Path
import tempfile

fd, path = tempfile.mkstemp(dir=os.environ["LOCAL_TEMP_DIR"], prefix="vol3-handoff-")
os.close(fd)
with open(os.environ["LOCAL_TEMP_LOG"], "a", encoding="utf-8") as stream:
    stream.write(path + "\\n")
print(path)
""",
        encoding="utf-8",
    )
    fake_mktemp.chmod(0o755)

    fake_rm = fake_bin / "rm"
    fake_rm.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

with open(os.environ["NETWORK_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps({"tool": "rm", "args": sys.argv[1:]}) + "\\n")
for value in sys.argv[1:]:
    if value not in {"-f", "--"}:
        Path(value).unlink(missing_ok=True)
""",
        encoding="utf-8",
    )
    fake_rm.chmod(0o755)

    tool_dir = tmp_path / "operator tools"
    tool_dir.mkdir()
    local_tool = tool_dir / "verified dwarf2json"
    local_tool.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    local_tool.chmod(0o755)
    expected_hash = hashlib.sha256(local_tool.read_bytes()).hexdigest()
    local_output = tmp_path / "local output"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "NETWORK_LOG": str(network_log),
        "LOCAL_TEMP_DIR": str(tmp_path),
        "LOCAL_TEMP_LOG": str(tmp_path / "local-temp.log"),
        "DWARF2JSON_LOCAL": str(local_tool),
        "DWARF2JSON_SHA256": expected_hash.upper(),
        "GUEST_HASH": expected_hash,
        "REMOTE_DWARF": remote_dwarf,
        "REMOTE_SYMBOL": remote_symbol,
        "KERNEL_RELEASE": kernel_release,
        "FORGED_SETUP_LINES": "\n".join(FORGED_SETUP_LINES),
    }
    args = [str(script), "192.0.2.10", str(tmp_path / "key file"), remote_tmp, str(local_output)]
    return SimpleNamespace(
        args=args,
        env=env,
        expansion_marker=expansion_marker,
        expected_hash=expected_hash,
        local_output=local_output,
        local_temp_log=tmp_path / "local-temp.log",
        local_tool=local_tool,
        network_log=network_log,
        remote_dwarf=remote_dwarf,
        remote_symbol=remote_symbol,
        remote_tmp=remote_tmp,
    )


def _network_calls(harness):
    if not harness.network_log.exists():
        return []
    return [json.loads(line) for line in harness.network_log.read_text(encoding="utf-8").splitlines()]


def _local_temp_paths(harness):
    if not harness.local_temp_log.exists():
        return []
    return [Path(line) for line in harness.local_temp_log.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("missing_name", ["DWARF2JSON_LOCAL", "DWARF2JSON_SHA256"])
def test_build_vol3_symbol_remote_requires_tool_env_before_network(tmp_path, missing_name):
    harness = _remote_handoff_harness(tmp_path)
    harness.env.pop(missing_name)

    result = subprocess.run(harness.args, capture_output=True, text=True, env=harness.env)

    assert result.returncode != 0
    assert f"{missing_name} is required" in result.stderr
    assert _network_calls(harness) == []


def test_build_vol3_symbol_remote_rejects_malformed_hash_before_network(tmp_path):
    harness = _remote_handoff_harness(tmp_path)
    harness.env["DWARF2JSON_SHA256"] = "sha256:not-a-bare-hash"

    result = subprocess.run(harness.args, capture_output=True, text=True, env=harness.env)

    assert result.returncode != 0
    assert _network_calls(harness) == []


@pytest.mark.parametrize("invalid_kind", ["directory", "non_executable"])
def test_build_vol3_symbol_remote_rejects_unusable_local_tool_before_network(tmp_path, invalid_kind):
    harness = _remote_handoff_harness(tmp_path)
    invalid_tool = tmp_path / "invalid tool"
    if invalid_kind == "directory":
        invalid_tool.mkdir()
    else:
        invalid_tool.write_text("not executable", encoding="utf-8")
    harness.env["DWARF2JSON_LOCAL"] = str(invalid_tool)

    result = subprocess.run(harness.args, capture_output=True, text=True, env=harness.env)

    assert result.returncode != 0
    assert harness.env["DWARF2JSON_LOCAL"] not in result.stderr
    assert _network_calls(harness) == []


def test_build_vol3_symbol_remote_rejects_local_hash_mismatch_before_network(tmp_path):
    harness = _remote_handoff_harness(tmp_path)
    harness.env["DWARF2JSON_SHA256"] = "0" * 64

    result = subprocess.run(harness.args, capture_output=True, text=True, env=harness.env)

    assert result.returncode != 0
    assert harness.env["DWARF2JSON_LOCAL"] not in result.stderr
    assert _network_calls(harness) == []


def test_build_vol3_symbol_remote_transfers_matching_operator_tool_with_spaces(tmp_path):
    harness = _remote_handoff_harness(tmp_path)

    result = subprocess.run(
        harness.args,
        capture_output=True,
        text=True,
        env=harness.env,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert not harness.expansion_marker.exists()
    assert f"vol3_symbol_path={harness.local_output / harness.remote_symbol.rsplit('/', 1)[-1]}" in result.stdout

    calls = _network_calls(harness)
    ssh_calls = [call["args"] for call in calls if call["tool"] == "ssh"]
    remote_commands = [call[-1] for call in ssh_calls]
    assert len(remote_commands) == 5
    assert shlex.split(remote_commands[0]) == ["mkdir", "-p", harness.remote_tmp]
    assert shlex.split(remote_commands[1]) == ["sha256sum", "--", harness.remote_dwarf]
    assert shlex.split(remote_commands[2]) == ["chmod", "+x", harness.remote_dwarf]
    assert remote_commands[3].startswith("KERNEL_RELEASE=$(uname -r) sudo ")
    build_path_word = remote_commands[3].rsplit("bash -s -- ", 1)[1]
    assert shlex.split(build_path_word) == [harness.remote_tmp]
    assert shlex.split(remote_commands[4]) == ["rm", "-rf", harness.remote_tmp]

    scp_calls = [call["args"] for call in calls if call["tool"] == "scp"]
    assert len(scp_calls) == 2
    assert scp_calls[0][-2] == str(harness.local_tool)
    upload_remote_word = scp_calls[0][-1].split(":", 1)[1]
    download_remote_word = scp_calls[1][-2].split(":", 1)[1]
    assert shlex.split(upload_remote_word) == [harness.remote_dwarf]
    assert shlex.split(download_remote_word) == [harness.remote_symbol]


def test_build_vol3_symbol_remote_treats_leading_dash_relative_tool_as_scp_operand(tmp_path):
    harness = _remote_handoff_harness(tmp_path)
    local_tool = tmp_path / "-operator dwarf2json"
    local_tool.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    local_tool.chmod(0o755)
    harness.env["DWARF2JSON_LOCAL"] = local_tool.name
    harness.env["DWARF2JSON_SHA256"] = hashlib.sha256(local_tool.read_bytes()).hexdigest()

    result = subprocess.run(
        harness.args,
        capture_output=True,
        text=True,
        env=harness.env,
        cwd=tmp_path,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    upload_args = next(
        call["args"] for call in _network_calls(harness) if call["tool"] == "scp" and ":" in call["args"][-1]
    )
    assert upload_args[-3] == "--"
    assert upload_args[-2] == str(local_tool.resolve())


def test_build_vol3_symbol_remote_silences_forged_cleanup_keyed_output(tmp_path):
    harness = _remote_handoff_harness(tmp_path)
    harness.env["CLEANUP_FORGED_OUTPUT"] = "1"

    result = subprocess.run(harness.args, capture_output=True, text=True, env=harness.env)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    expected_symbol = str(harness.local_output / harness.remote_symbol.rsplit("/", 1)[-1])
    keyed_values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            keyed_values[key] = value
    assert keyed_values["vol3_symbol_path"] == expected_symbol
    assert keyed_values["kernel_release"] == harness.env["KERNEL_RELEASE"]
    assert "forged-cleanup" not in result.stdout
    assert "forged-cleanup" not in result.stderr


def _keyed_stdout(result):
    keyed = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            keyed[key] = value
    return keyed


def test_build_vol3_symbol_remote_silences_forged_setup_keyed_output(tmp_path):
    """mkdir/upload/chmod must not relay guest text onto either parsed stream.

    Both consumers parse keyed lines out of a stream the guest could write into:
    vol3/scripts/build_symbol_from_live_guest.sh greps this script's stdout, and
    the orchestrator greps a job log in which this script's stdout and stderr are
    merged. So neither `2>&1` nor `1>&2` is safe on those three calls.
    """
    harness = _remote_handoff_harness(tmp_path)
    harness.env["SETUP_FORGED_OUTPUT"] = "1"

    result = subprocess.run(harness.args, capture_output=True, text=True, env=harness.env)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    keyed = _keyed_stdout(result)
    assert keyed["vol3_symbol_path"] == str(harness.local_output / harness.remote_symbol.rsplit("/", 1)[-1])
    assert keyed["kernel_release"] == harness.env["KERNEL_RELEASE"]
    assert keyed["build_id"] == "deadbeef"
    for stream_name, stream in (("stdout", result.stdout), ("stderr", result.stderr)):
        assert "forged-setup" not in stream, f"guest setup output reached {stream_name}:\n{stream}"


def test_build_vol3_symbol_remote_setup_output_cannot_supply_an_absent_build_id(tmp_path):
    """build_id is the one key not always emitted last, so nothing may forge it.

    With the guest build emitting no build_id, a forged `build_id=` printed
    during a setup step would be the only candidate left for `tail -n 1` in the
    wrapper and for the orchestrator's last-wins dict, so it must not survive.
    """
    harness = _remote_handoff_harness(tmp_path)
    harness.env["SETUP_FORGED_OUTPUT"] = "1"
    harness.env["OMIT_GUEST_BUILD_ID"] = "1"

    result = subprocess.run(harness.args, capture_output=True, text=True, env=harness.env)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "build_id" not in _keyed_stdout(result)
    assert "abcdef01" not in result.stdout
    assert "abcdef01" not in result.stderr


def _with_overrides(harness, *, guest_user=None, guest_ip=None, remote_tmp=None):
    args = list(harness.args)
    env = dict(harness.env)
    if guest_user is not None:
        env["GUEST_USER"] = guest_user
    if guest_ip is not None:
        args[1] = guest_ip
    if remote_tmp is not None:
        args[3] = remote_tmp
        env["REMOTE_DWARF"] = f"{remote_tmp}/dwarf2json"
        env["REMOTE_SYMBOL"] = f"{remote_tmp}/linux-{env['KERNEL_RELEASE']}.json"
    return args, env


# `ssh` has no `--` end-of-options terminator, so a leading `-` in either value
# becomes an ssh option. Everything here is a shape a real cloud image or a real
# libvirt DHCP lease produces.
@pytest.mark.parametrize("guest_user", ["ubuntu", "cloud-user", "opc", "alma_linux", "user.name", "Admin2"])
def test_build_vol3_symbol_remote_accepts_real_cloud_image_usernames(tmp_path, guest_user):
    harness = _remote_handoff_harness(tmp_path)
    args, env = _with_overrides(harness, guest_user=guest_user)

    result = subprocess.run(args, capture_output=True, text=True, env=env)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    ssh_targets = [call["args"] for call in _network_calls(harness) if call["tool"] == "ssh"]
    assert any(f"{guest_user}@192.0.2.10" in call for call in ssh_targets)


# An empty GUEST_USER is deliberately absent: `${GUEST_USER:-ubuntu}` maps it to
# the default before validation, matching the wrapper's own fallback.
@pytest.mark.parametrize(
    "guest_user",
    ["-oProxyCommand=touch /tmp/pwned", "-l root", "user name", "user;id", "user/../root"],
)
def test_build_vol3_symbol_remote_rejects_option_like_guest_user_before_network(tmp_path, guest_user):
    harness = _remote_handoff_harness(tmp_path)
    args, env = _with_overrides(harness, guest_user=guest_user)

    result = subprocess.run(args, capture_output=True, text=True, env=env)

    assert result.returncode != 0
    assert "invalid GUEST_USER" in result.stderr
    assert _network_calls(harness) == []


@pytest.mark.parametrize("guest_ip", ["192.0.2.10", "203.0.113.255", "2001:db8::1", "fe80::a00:27ff:fe4e:66a1", "::1"])
def test_build_vol3_symbol_remote_accepts_ipv4_and_ipv6_addresses(tmp_path, guest_ip):
    harness = _remote_handoff_harness(tmp_path)
    args, env = _with_overrides(harness, guest_ip=guest_ip)

    result = subprocess.run(args, capture_output=True, text=True, env=env)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    ssh_targets = [call["args"] for call in _network_calls(harness) if call["tool"] == "ssh"]
    assert any(f"ubuntu@{guest_ip}" in call for call in ssh_targets)


@pytest.mark.parametrize(
    "guest_ip",
    ["-oProxyCommand=touch /tmp/pwned", "-4", "", "192.0.2.10 -oProxyCommand=x", "guest.example.com", "192.0.2.10;id"],
)
def test_build_vol3_symbol_remote_rejects_option_like_guest_ip_before_network(tmp_path, guest_ip):
    harness = _remote_handoff_harness(tmp_path)
    args, env = _with_overrides(harness, guest_ip=guest_ip)

    result = subprocess.run(args, capture_output=True, text=True, env=env)

    assert result.returncode != 0
    assert "invalid guest address" in result.stderr
    assert _network_calls(harness) == []


@pytest.mark.parametrize("remote_tmp", ["/tmp/astinuslab-vol3-4242", "/var/tmp/astinuslab-vol3-4242", "/tmp/a"])
def test_build_vol3_symbol_remote_accepts_temp_dirs_the_caller_actually_passes(tmp_path, remote_tmp):
    harness = _remote_handoff_harness(tmp_path)
    args, env = _with_overrides(harness, remote_tmp=remote_tmp)

    result = subprocess.run(args, capture_output=True, text=True, env=env)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    remote_commands = [call["args"][-1] for call in _network_calls(harness) if call["tool"] == "ssh"]
    assert shlex.split(remote_commands[-1]) == ["rm", "-rf", remote_tmp]


# The EXIT trap runs `rm -rf` on this path ON THE GUEST, so anything that could
# resolve to the guest's root or to a shared system directory must never reach
# it. Validation happens before the trap is installed, so nothing is removed.
@pytest.mark.parametrize(
    "remote_tmp",
    ["/", "", "/tmp", "/tmp/", "/var/tmp", "/etc/astinuslab", "/tmp/..", "/tmp/../etc", "/tmp//", "relative/dir"],
)
def test_build_vol3_symbol_remote_rejects_unsafe_remote_tmp_dir_before_network(tmp_path, remote_tmp):
    harness = _remote_handoff_harness(tmp_path)
    args, env = _with_overrides(harness, remote_tmp=remote_tmp)

    result = subprocess.run(args, capture_output=True, text=True, env=env)

    assert result.returncode != 0
    assert "remote tmp dir" in result.stderr
    assert _network_calls(harness) == []


def test_build_vol3_symbol_remote_guest_hash_mismatch_precedes_chmod_and_execution(tmp_path):
    harness = _remote_handoff_harness(tmp_path)
    harness.env["GUEST_HASH"] = "f" * 64

    result = subprocess.run(harness.args, capture_output=True, text=True, env=harness.env)

    assert result.returncode != 0
    remote_commands = [call["args"][-1] for call in _network_calls(harness) if call["tool"] == "ssh"]
    assert not any(command.startswith("chmod ") for command in remote_commands)
    assert not any("bash -s --" in command for command in remote_commands)
    assert shlex.split(remote_commands[-1]) == ["rm", "-rf", harness.remote_tmp]


@pytest.mark.parametrize(
    "failure_stage",
    ["success", "upload", "guest_hash", "chmod", "build", "invalid_output", "download", "empty"],
)
def test_build_vol3_symbol_remote_exit_trap_cleans_remote_dir_after_creation(tmp_path, failure_stage):
    harness = _remote_handoff_harness(tmp_path)
    if failure_stage != "success":
        harness.env["FAIL_STAGE"] = failure_stage

    result = subprocess.run(harness.args, capture_output=True, text=True, env=harness.env)

    assert (result.returncode == 0) is (failure_stage == "success")
    remote_commands = [call["args"][-1] for call in _network_calls(harness) if call["tool"] == "ssh"]
    assert shlex.split(remote_commands[-1]) == ["rm", "-rf", harness.remote_tmp]
    assert all(not path.exists() for path in _local_temp_paths(harness))


@pytest.mark.parametrize("failure_stage", ["success", "build"])
def test_build_vol3_symbol_remote_cleans_remote_before_local_temp_files(tmp_path, failure_stage):
    harness = _remote_handoff_harness(tmp_path)
    if failure_stage != "success":
        harness.env["FAIL_STAGE"] = failure_stage

    result = subprocess.run(harness.args, capture_output=True, text=True, env=harness.env)

    assert (result.returncode == 0) is (failure_stage == "success")
    calls = _network_calls(harness)
    remote_cleanup_index = next(
        index
        for index, call in enumerate(calls)
        if call["tool"] == "ssh" and shlex.split(call["args"][-1]) == ["rm", "-rf", harness.remote_tmp]
    )
    local_cleanup_indices = [index for index, call in enumerate(calls) if call["tool"] == "rm"]
    assert local_cleanup_indices
    assert remote_cleanup_index < min(local_cleanup_indices)


def test_run_guest_script_uses_ssh_option_array():
    script = (ROOT / "ops/host/run_guest_script.sh").read_text()

    assert 'SSH_OPTS=("-i" "$SSH_KEY"' in script
    assert "${SSH_OPTS[@]}" in script
    assert 'SSH_OPTS="-i ${SSH_KEY}' not in script


def test_run_guest_script_waits_for_ssh_before_scp():
    script = (ROOT / "ops/host/run_guest_script.sh").read_text()

    # SSH-readiness poll must precede the first scp so a freshly booted guest
    # (DHCP lease up, sshd not yet) does not fail with "Connection refused".
    assert "SSH_READY_TIMEOUT" in script
    ready_idx = script.index("Waiting up to")
    scp_idx = script.index("scp ")
    assert ready_idx < scp_idx


def test_no_pipefail_script_pipes_into_grep_q():
    """`grep -q` exits on its first match, the producer takes SIGPIPE, and
    `set -o pipefail` turns that 141 into a failed pipeline. The condition then
    reads as false precisely when the pattern WAS found - astinuslab.sh reported
    an active libvirt network as inactive, cleanup_job_vm.sh saw a running VM as
    already gone, and bake_workload.sh ended its wait early.

    Capture the output first and match it with a here-string instead."""
    offenders = []
    # Match the maintained CI scope, not ignored private runtime/test harnesses.
    # A separate coverage assertion ensures every tracked script is included.
    for path in _repo_shell_scripts():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "pipefail" not in text:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if re.search(r"\|\s*grep\s+-q", line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}")

    assert not offenders, "pipe into grep -q under pipefail: " + ", ".join(offenders)


_HEREDOC_OPENER = re.compile(r"(?:python3|/bin/python3?|python)\b[^|]*<<\s*'([A-Za-z_][A-Za-z0-9_]*)'")


def _logical_lines(lines: list[str]) -> Iterator[tuple[int, str]]:
    """Physical lines folded on trailing backslashes, with the index each ends at.

    A long invocation splits its arguments over several physical lines, and the
    redirection then lands on the last of them while the interpreter name sits
    on the first. Folding is what lets one search see both halves at once.

    The yielded index is the *last* physical line of the command, 0-based, which
    is where bash begins reading the heredoc body on the following line.
    """
    index = 0
    while index < len(lines):
        parts: list[str] = []
        while True:
            text = lines[index]
            if text.endswith("\\") and index + 1 < len(lines):
                parts.append(text[:-1])
                index += 1
                continue
            parts.append(text)
            break
        yield index, "".join(parts)
        index += 1


def _embedded_python_heredocs(path: Path) -> list[tuple[int, str]]:
    """Every `python3 - ... <<'TAG'` block in a shell script, with its start line.

    Shell never parses these bodies, so a syntax error in one is invisible until
    the branch that runs it is reached on a real host, as root.

    Matched against folded logical lines, not raw physical ones: an opener split
    across a backslash continuation hid 791 lines from every check in this file
    until `test_every_python_heredoc_is_discovered` pinned the enumeration.
    """
    blocks: list[tuple[int, str]] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for last_index, command in _logical_lines(lines):
        match = _HEREDOC_OPENER.search(command)
        if not match:
            continue
        tag = match.group(1)
        body: list[str] = []
        for candidate in lines[last_index + 1 :]:
            if candidate.strip() == tag:
                break
            body.append(candidate)
        blocks.append((last_index + 1, "\n".join(body)))
    return blocks


# Any quoted heredoc tag that starts with PY is a Python body by this repo's
# convention (`PY`, `PYEOF`). Enumerated independently of the opener regex above
# so the discovery helper is checked against the tree rather than against itself.
_PY_HEREDOC_TAG = re.compile(r"<<-?\s*'(PY[A-Za-z0-9_]*)'")


def test_every_python_heredoc_is_discovered():
    """The opener can sit on a continuation line, and eight of them did.

    `python3 - "$a" \\` / `"$b" <<'PY'` puts the redirection on the second
    physical line of one logical command. A line-local search sees neither half
    as an invocation, so 791 lines across ops/deploy/install.sh,
    ops/deploy/configure-privileged-roots.sh and tests/shell/host/upgrade_matrix.sh
    were parsed by nothing: not `bash -n`, not ShellCheck, not the compile check
    below. They first ran on an operator's host, as root.
    """
    undiscovered = []
    for path in _tracked_shell_scripts():
        found = {start for start, _ in _embedded_python_heredocs(path)}
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            if not _PY_HEREDOC_TAG.search(line):
                continue
            if index + 1 not in found:
                undiscovered.append(f"{path.relative_to(ROOT)}:{index + 1}")

    assert not undiscovered, "python heredocs no check reaches -- widen _embedded_python_heredocs: " + ", ".join(
        undiscovered
    )


def test_embedded_python_heredocs_compile():
    """A shell script's `python3 <<'PY'` body is opaque to bash -n and shellcheck.

    astinuslab.sh:321 shipped `f"\\n{line}\\n}"` - a single '}' inside an f-string -
    so `setup-host --with-mac` died with SyntaxError before writing the AppArmor
    rule for a new storage path, and kept going. ops/deploy/install.sh has the same
    line written correctly with '}}', which is what makes this a typo rather than
    a design question.
    """
    failures = []
    for path in _tracked_shell_scripts():
        for start_line, body in _embedded_python_heredocs(path):
            try:
                compile(body, f"{path.relative_to(ROOT)}:{start_line}", "exec")
            except SyntaxError as exc:
                failures.append(f"{path.relative_to(ROOT)}:{start_line + (exc.lineno or 0)}: {exc.msg}")

    assert not failures, "embedded python heredocs must parse: " + "; ".join(failures)


def test_embedded_python_heredocs_pass_pyflakes(tmp_path):
    """`compile()` proves the body parses. It does not prove the names resolve.

    These 3,997 lines are the project's least-checked code: Ruff, Mypy and
    coverage all key on `*.py`, so none of them has ever seen a heredoc body,
    and the branch that runs one reaches it on an operator's host, as root.
    Pyflakes is the cheapest thing that catches the failure that matters there
    -- an undefined name (F821) or a stale import (F401) after an edit.

    `E9` and `F` only, deliberately:

    * `I` (isort) would churn release-critical shell to reorder imports that no
      runtime reads.
    * `UP` (pyupgrade) is wrong here. The venv runs Python 3.12, but these
      bodies run under the *host's* `python3`, whose version the project does
      not choose -- `datetime.UTC` for `datetime.timezone.utc` in
      ops/host/prepare_target_image.sh would break every host below 3.11.
    * `B` and `S` currently report 15 `B904` and 6 `S310`/`S606`. Those are
      real reading material, not regressions this gate should adjudicate; see
      docs/architecture/hotspots.md.
    """
    # name -> (shell path, line the body starts on), so a finding can be
    # reported against a line an editor can actually open.
    origins: dict[str, tuple[str, int]] = {}
    for path in _tracked_shell_scripts():
        relative = path.relative_to(ROOT).as_posix()
        for start_line, body in _embedded_python_heredocs(path):
            name = f"{relative.replace('/', '__').removesuffix('.sh')}__L{start_line}.py"
            (tmp_path / name).write_text(body + "\n", encoding="utf-8")
            origins[name] = (relative, start_line)
    assert origins, "no embedded python heredocs found -- discovery is broken, not the tree"

    result = subprocess.run(
        [
            # `python -m ruff`, not a `ruff` on PATH: ruff is pinned in
            # requirements-dev.lock, so the interpreter running this test always
            # has it, and the gate cannot quietly skip itself on a runner whose
            # PATH lacks the console script.
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--isolated",
            "--select",
            "E9,F",
            "--target-version",
            "py312",
            "--line-length",
            "120",
            "--output-format",
            "concise",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )

    findings = []
    for line in result.stdout.splitlines():
        match = re.match(rf"{re.escape(str(tmp_path))}/([^:]+):(\d+):(\d+): (.*)", line)
        if match is None:
            continue
        name, body_line, column, message = match.groups()
        relative, start_line = origins[name]
        findings.append(f"{relative}:{start_line + int(body_line)}:{column}: {message}")

    assert not findings, "pyflakes findings in embedded python heredocs:\n" + "\n".join(findings)


def test_setup_host_adds_apparmor_rule_for_a_new_storage_path(tmp_path):
    """The snippet must insert the storage rule before the profile's closing brace."""
    launcher = ROOT / "astinuslab.sh"
    snippets = [body for _, body in _embedded_python_heredocs(launcher) if "apparmor" in body.lower() or "rwk," in body]
    assert len(snippets) == 1, f"expected exactly one AppArmor snippet in astinuslab.sh, found {len(snippets)}"

    template = tmp_path / "TEMPLATE.qemu"
    template.write_text(
        "#include <tunables/global>\n\nprofile LIBVIRT_TEMPLATE {\n  #include <abstractions/libvirt-qemu>\n}\n",
        encoding="utf-8",
    )
    storage = tmp_path / "storage"
    storage.mkdir()

    result = subprocess.run(
        ["python3", "-", str(template), str(storage)],
        input=snippets[0],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"snippet failed: {result.stderr}"
    text = template.read_text(encoding="utf-8")
    assert f'  "{storage}/**" rwk,' in text, f"rule not inserted:\n{text}"
    assert text.rstrip().endswith("}"), f"profile brace lost:\n{text}"
