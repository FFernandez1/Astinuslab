from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _live_guest_wrapper_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Copy the wrapper into a temp tree whose child script only dumps its environment.

    The wrapper resolves the remote script relative to its own location
    (``$BASE_DIR/ops/host/build_vol3_symbol_remote.sh``), so a temp copy of
    the tree layout is what puts the stub in the child's place.
    """
    tree = tmp_path / "tree"
    (tree / "vol3" / "scripts").mkdir(parents=True)
    (tree / "ops" / "host").mkdir(parents=True)

    wrapper = tree / "vol3" / "scripts" / "build_symbol_from_live_guest.sh"
    shutil.copy2(_repo_root() / "vol3" / "scripts" / "build_symbol_from_live_guest.sh", wrapper)

    env_dump = tmp_path / "child-env.txt"
    child = tree / "ops" / "host" / "build_vol3_symbol_remote.sh"
    child.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"env -0 > {env_dump}\n"
        'mkdir -p -- "$4"\n'
        "printf '{}\\n' > \"$4/linux-6.8.0-test.json\"\n"
        'echo "vol3_symbol_path=$4/linux-6.8.0-test.json"\n'
        "echo 'kernel_release=6.8.0-test'\n",
        encoding="utf-8",
    )
    child.chmod(0o755)
    return wrapper, child, env_dump


# A space and mixed case on purpose: the child must receive these byte-for-byte,
# not re-split, re-quoted, lowercased, or otherwise normalized by the wrapper.
OPERATOR_SHA256 = "E2E75BA5BC9C22BC38A48EDFFBB050456D089D54998D54F46CB580223D9FBC6B"
OPERATOR_GUEST_USER = "forensics operator"


def _run_live_guest_wrapper(tmp_path: Path, wrapper: Path, **tool_env: str | None):
    ssh_key = tmp_path / "guest key"
    ssh_key.write_text("not a real key\n", encoding="utf-8")
    # GUEST_USER is deliberately absent from the environment and supplied only as
    # a flag: the wrapper holds it in a plain shell variable, so it can reach the
    # child solely through the assignment prefixes under test.
    env = {
        name: value for name, value in os.environ.items() if not name.startswith(("DWARF2JSON_", "GUEST_USER", "VOL3_"))
    }
    env.update({name: value for name, value in tool_env.items() if value is not None})
    return subprocess.run(
        [
            "bash",
            str(wrapper),
            "--guest-ip",
            "192.0.2.10",
            "--ssh-key",
            str(ssh_key),
            "--guest-user",
            OPERATOR_GUEST_USER,
            "--work-dir",
            str(tmp_path / "work dir"),
            "--symbols-dir",
            str(tmp_path / "symbols dir"),
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def test_live_guest_wrapper_passes_operator_tool_env_to_the_child_process(tmp_path):
    wrapper, _child, env_dump = _live_guest_wrapper_tree(tmp_path)
    operator_tool = str(tmp_path / "operator tools" / "verified dwarf2json")

    result = _run_live_guest_wrapper(
        tmp_path,
        wrapper,
        DWARF2JSON_LOCAL=operator_tool,
        DWARF2JSON_SHA256=OPERATOR_SHA256,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert env_dump.is_file(), "child script never ran"
    child_env = dict(entry.split("=", 1) for entry in env_dump.read_bytes().decode().split("\0") if "=" in entry)
    assert child_env["DWARF2JSON_LOCAL"] == operator_tool
    assert child_env["DWARF2JSON_SHA256"] == OPERATOR_SHA256
    # Only the wrapper's assignment prefixes can deliver these two, so this pins
    # the propagation block itself rather than plain environment inheritance.
    assert child_env["GUEST_USER"] == OPERATOR_GUEST_USER
    assert child_env["VOL3_ALLOW_GUEST_PACKAGE_INSTALL"] == "0"


@pytest.mark.parametrize("missing_name", ["DWARF2JSON_LOCAL", "DWARF2JSON_SHA256"])
@pytest.mark.parametrize("missing_value", ["", None], ids=["empty", "unset"])
def test_live_guest_wrapper_aborts_before_child_without_operator_tool_env(tmp_path, missing_name, missing_value):
    wrapper, _child, env_dump = _live_guest_wrapper_tree(tmp_path)
    tool_env: dict[str, str | None] = {
        "DWARF2JSON_LOCAL": str(tmp_path / "operator tools" / "verified dwarf2json"),
        "DWARF2JSON_SHA256": OPERATOR_SHA256,
        missing_name: missing_value,
    }

    result = _run_live_guest_wrapper(tmp_path, wrapper, **tool_env)

    assert result.returncode != 0
    assert f"{missing_name} is required" in result.stderr
    assert not env_dump.exists(), "child script ran without complete operator tool configuration"


# Keyed lines a compromised guest tries to smuggle out of a setup step. These
# are the orchestrator's key names (vol3_support._parse_keyed_output), plus the
# host script's own, so the assertion covers both parsing layers.
FORGED_GUEST_LINES = (
    "isf_path=/tmp/forged-guest.json",
    "vol3_symbol_path=/tmp/forged-guest.json",
    "kernel_release=forged-guest",
    "build_id=abcdef01",
)
FORGED_GUEST_MARKER = "forged-guest"

_FAKE_SSH = """#!/usr/bin/env python3
import os
import shlex
import sys


def forge():
    forged = os.environ.get("FORGED_GUEST_LINES", "")
    if not forged:
        return
    for line in forged.split("\\n"):
        print(line)
    print("forged-guest-on-stderr", file=sys.stderr)


command = sys.argv[-1]
if command.startswith("mkdir ") or command.startswith("chmod "):
    forge()
elif command.startswith("sha256sum "):
    print(f"{os.environ['GUEST_HASH']}  {shlex.split(command)[-1]}")
elif "bash -s -- " in command:
    remote_tmp = shlex.split(command.rsplit("bash -s -- ", 1)[1])[0]
    # Deliberately no build_id: it is the one key the trusted scripts do not
    # always emit, so it is the one a forged setup line could still win.
    print(f"vol3_symbol_remote_path={remote_tmp}/linux-{os.environ['KERNEL_RELEASE']}.json")
    print(f"kernel_release={os.environ['KERNEL_RELEASE']}")
"""

_FAKE_SCP = """#!/usr/bin/env python3
import os
from pathlib import Path
import sys

if ":" in sys.argv[-1]:
    forged = os.environ.get("FORGED_GUEST_LINES", "")
    if forged:
        for line in forged.split("\\n"):
            print(line)
        print("forged-guest-on-stderr", file=sys.stderr)
    raise SystemExit(0)
Path(sys.argv[-1]).write_text("{}\\n", encoding="utf-8")
"""


def test_live_guest_handoff_keeps_forged_setup_lines_out_of_the_parsed_job_log(tmp_path):
    """The three setup steps must not reach the keys the orchestrator parses.

    ``run_live_symbol_build`` runs this wrapper with ``stdout=lf, stderr=lf``,
    so the job log it later feeds to ``_parse_keyed_output`` is the wrapper's
    two streams merged. The guest gets to write during mkdir, the dwarf2json
    upload and the chmod — before any trusted keyed line exists — so those three
    calls must relay guest bytes onto neither stream.

    Scope: this covers the setup steps only. The build step further down
    deliberately relays the guest script's stderr for operator diagnostics
    (``2> >(tee "$GUEST_STDERR_FILE" >&2)``), so a guest still has a channel
    into the merged log there; ``build_id`` is treated as guest-sourced
    downstream for exactly that reason.
    """
    from astinuslab.forensics.vol3_support import _parse_keyed_output

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in (("ssh", _FAKE_SSH), ("scp", _FAKE_SCP)):
        tool = fake_bin / name
        tool.write_text(body, encoding="utf-8")
        tool.chmod(0o755)

    dwarf2json = tmp_path / "dwarf2json"
    dwarf2json.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    dwarf2json.chmod(0o755)
    import hashlib

    digest = hashlib.sha256(dwarf2json.read_bytes()).hexdigest()

    ssh_key = tmp_path / "guest key"
    ssh_key.write_text("not a real key\n", encoding="utf-8")
    symbols_dir = tmp_path / "symbols dir"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DWARF2JSON_LOCAL": str(dwarf2json),
        "DWARF2JSON_SHA256": digest,
        "GUEST_HASH": digest,
        "KERNEL_RELEASE": "6.8.0-test",
        "FORGED_GUEST_LINES": "\n".join(FORGED_GUEST_LINES),
    }

    result = subprocess.run(
        [
            "bash",
            str(_repo_root() / "vol3" / "scripts" / "build_symbol_from_live_guest.sh"),
            "--guest-ip",
            "192.0.2.10",
            "--ssh-key",
            str(ssh_key),
            "--guest-user",
            "cloud-user",
            "--work-dir",
            str(tmp_path / "work dir"),
            "--symbols-dir",
            str(symbols_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    job_log = tmp_path / "job.log"
    job_log.write_text(result.stdout + result.stderr, encoding="utf-8")
    assert FORGED_GUEST_MARKER not in job_log.read_text(encoding="utf-8"), job_log.read_text(encoding="utf-8")

    parsed = _parse_keyed_output(job_log)
    assert parsed["isf_path"] == str(symbols_dir / "linux" / "linux-6.8.0-test.json")
    assert parsed["kernel_release"] == "6.8.0-test"
    assert "build_id" not in parsed, "a forged setup line supplied the one key the trusted scripts omitted"


def test_remote_symbol_script_never_relays_guest_setup_output_to_stdout():
    """mkdir/upload/chmod must not end in `2>&1` (our stdout) or `1>&2` (our stderr)."""
    script = (_repo_root() / "ops" / "host" / "build_vol3_symbol_remote.sh").read_text(encoding="utf-8")

    import re

    # An ssh/scp call whose output is not captured leaks guest bytes onto one of
    # our two streams, whichever way the redirection points.
    uncaptured = [
        line
        for line in script.splitlines()
        if re.match(r'\s*(if !\s+)?("\$\{SSH_COMMAND\[@\]\}"|scp)\s', line) and re.search(r"(2>&1|1>&2)\s*;?\s*$", line)
    ]
    assert not uncaptured, f"guest output is relayed to one of our streams: {uncaptured}"
    assert 'REMOTE_MKDIR_OUTPUT="$("${SSH_COMMAND[@]}" "$REMOTE_MKDIR_COMMAND" 2>&1)"' in script
    assert 'REMOTE_CHMOD_OUTPUT="$("${SSH_COMMAND[@]}" "$REMOTE_CHMOD_COMMAND" 2>&1)"' in script
    assert 'SCP_UPLOAD_OUTPUT="$(scp "${SSH_OPTS[@]}" -- \\' in script


def test_remote_symbol_script_validates_ssh_target_and_remote_tmp_dir():
    """`ssh` has no `--` terminator, so the values themselves must be checked."""
    script = (_repo_root() / "ops" / "host" / "build_vol3_symbol_remote.sh").read_text(encoding="utf-8")

    assert '"$GUEST_USER" =~ ^[A-Za-z0-9._][A-Za-z0-9._-]*$' in script
    assert '"$GUEST_IP" =~ ^[0-9a-fA-F.:]+$' in script
    assert "/tmp/?* | /var/tmp/?*" in script
    assert "*/../* | */.. | *//*" in script


def test_current_vol3_scripts_are_present():
    repo_root = _repo_root()
    scripts_dir = repo_root / "vol3" / "scripts"

    assert (scripts_dir / "fetch_vmlinux.sh").is_file()
    assert (scripts_dir / "build_symbol_from_live_guest.sh").is_file()


def test_current_vol3_scripts_parse_as_bash():
    repo_root = _repo_root()
    scripts = [
        repo_root / "vol3" / "scripts" / "fetch_vmlinux.sh",
        repo_root / "vol3" / "scripts" / "build_symbol_from_live_guest.sh",
        repo_root / "ops" / "host" / "build_vol3_symbol_remote.sh",
        repo_root / "guest" / "scripts" / "build_vol3_symbol.sh",
    ]

    for script in scripts:
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_fetch_vmlinux_validates_args_without_unbound_variables(tmp_path):
    repo_root = _repo_root()
    script = repo_root / "vol3" / "scripts" / "fetch_vmlinux.sh"

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--work-dir",
            str(tmp_path / "work"),
            "--symbols-dir",
            str(tmp_path / "symbols"),
            "--distro",
            "unsupported",
            "--version",
            "1",
            "--kernel-release",
            "1.2.3-test",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "unbound variable" not in result.stderr


def test_fetch_vmlinux_exposes_current_direct_symbol_flags():
    repo_root = _repo_root()
    script = (repo_root / "vol3" / "scripts" / "fetch_vmlinux.sh").read_text(encoding="utf-8")

    for flag in ["--work-dir", "--symbols-dir", "--distro", "--version", "--kernel-release", "--arch"]:
        assert flag in script


def test_fetch_vmlinux_uses_https_only():
    repo_root = _repo_root()
    script = (repo_root / "vol3" / "scripts" / "fetch_vmlinux.sh").read_text(encoding="utf-8")

    assert "http://" not in script


def test_fetch_vmlinux_downloads_are_resumable_and_retried():
    repo_root = _repo_root()
    script = (repo_root / "vol3" / "scripts" / "fetch_vmlinux.sh").read_text(encoding="utf-8")

    assert "DOWNLOAD_ATTEMPTS" in script
    assert "wget -q --show-progress -c -O" in script
    assert "curl -fL -C - --progress-bar -o" in script
    assert "download attempt ${attempt}/${DOWNLOAD_ATTEMPTS}" in script


def test_fetch_vmlinux_codename_map_has_no_placeholder_entries():
    """B02: bogus codenames defeat Launchpad discovery; unmapped releases stay absent."""
    text = (_repo_root() / "vol3" / "scripts" / "fetch_vmlinux.sh").read_text(encoding="utf-8")
    assert '"unknown"' not in text
    assert '"26.04"' not in text


def test_fetch_vmlinux_codename_map_corrections():
    text = (_repo_root() / "vol3" / "scripts" / "fetch_vmlinux.sh").read_text(encoding="utf-8")
    assert '"25.10": "questing"' in text
    assert '"24.10": "oracular"' in text
    assert text.count('"oracular"') == 1


def test_build_symbol_from_live_guest_exposes_guest_user_flag():
    repo_root = _repo_root()
    script = (repo_root / "vol3" / "scripts" / "build_symbol_from_live_guest.sh").read_text(encoding="utf-8")

    assert "--guest-user" in script
    assert 'GUEST_USER="${GUEST_USER:-ubuntu}"' in script


def test_live_guest_handoff_uses_only_operator_selected_dwarf2json():
    repo_root = _repo_root()
    host_script = (repo_root / "ops" / "host" / "build_vol3_symbol_remote.sh").read_text(encoding="utf-8")
    wrapper = (repo_root / "vol3" / "scripts" / "build_symbol_from_live_guest.sh").read_text(encoding="utf-8")

    for script in (host_script, wrapper):
        assert "vol3/bin" not in script
        assert "dwarf2json-linux-amd64.sha256" not in script
    assert 'DWARF2JSON_LOCAL="$DWARF2JSON_LOCAL"' in wrapper
    assert 'DWARF2JSON_SHA256="$DWARF2JSON_SHA256"' in wrapper
    assert 'analysis_log "DWARF2JSON_LOCAL' not in wrapper


def test_remote_symbol_script_parsing_does_not_exit_before_diagnostics():
    repo_root = _repo_root()
    script = (repo_root / "ops" / "host" / "build_vol3_symbol_remote.sh").read_text(encoding="utf-8")

    assert (
        'VOL3_REMOTE_PATH="$(echo "$GUEST_OUTPUT" | grep \'^vol3_symbol_remote_path=\' | '
        'tail -n 1 | cut -d= -f2- || true)"'
    ) in script
    assert (
        'KERNEL_RELEASE="$(echo "$GUEST_OUTPUT" | grep \'^kernel_release=\' | tail -n 1 | cut -d= -f2- || true)"'
    ) in script
    assert "ERROR: guest script did not emit kernel_release" in script


def test_guest_live_symbol_build_is_x86_64_only():
    repo_root = _repo_root()
    script = (repo_root / "guest" / "scripts" / "build_vol3_symbol.sh").read_text(encoding="utf-8")

    assert "uname -m" in script
    assert "x86_64" in script


def test_guest_script_fallback_order():
    text = (_repo_root() / "guest" / "scripts" / "build_vol3_symbol.sh").read_text(encoding="utf-8")
    order = [
        "_try_existing_pkg",
        "_try_debuginfod",
        "_install_debug_pkg_apt",
        "_install_debug_pkg_dnf",
        "_install_debug_pkg_zypper",
        "_install_debug_pkg_pacman",
        "_install_debug_pkg_apk",
    ]
    positions = [text.index(name) for name in order]
    assert positions == sorted(positions), "fallback helpers must appear in declared order"


def test_guest_script_build_id_prefers_sysfs_notes():
    text = (_repo_root() / "guest" / "scripts" / "build_vol3_symbol.sh").read_text(encoding="utf-8")

    assert "/sys/kernel/notes" in text
    assert "extract-vmlinux" in text
    assert "readelf -n /boot/vmlinuz" not in text


def test_ubuntu_lookup_retries_then_succeeds(tmp_path, monkeypatch):
    """LP query fails twice, succeeds on third — script must not die."""
    import os

    repo_root = _repo_root()
    script = repo_root / "vol3" / "scripts" / "fetch_vmlinux.sh"

    # Stub python3 to fail twice then print a valid line
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    counter_file = tmp_path / "count"
    counter_file.write_text("0")

    py_stub = stub_dir / "python3"
    py_stub.write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(cat "{counter_file}")\n'
        f'echo $((n+1)) > "{counter_file}"\n'
        # codename mapping call (single arg = VERSION) returns "" so we exercise lookup
        'if [[ $# -eq 1 && "$1" =~ ^[0-9.]+$ ]]; then echo ""; exit 0; fi\n'
        'if [[ $n -lt 2 ]]; then echo "Launchpad fail" >&2; exit 1; fi\n'
        'echo "plucky|6.14.0-36.36|linux|linux-image-unsigned-6.14.0-36-generic-dbgsym"\n'
    )
    py_stub.chmod(0o755)

    env = {
        **dict(os.environ),
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "LP_QUERY_ATTEMPTS": "3",
        "DOWNLOAD_ATTEMPTS": "1",
    }
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    # We only verify the lookup retry by sourcing the function and calling it.
    result = subprocess.run(
        ["bash", "-c", f"set --; source {script}; _ubuntu_lookup '' amd64 ''"],
        capture_output=True,
        text=True,
        env=env,
    )
    # Function exits 0 on success and prints the pipe-delimited line on stdout
    assert result.returncode == 0, result.stderr
    assert "plucky|6.14.0-36.36|linux|" in result.stdout
    assert counter_file.read_text().strip() == "3"


def test_no_writes_to_etc_apt_when_fetching_ubuntu(tmp_path):
    """Host fetch path must not write to /etc/apt — must avoid apt entirely."""
    repo_root = _repo_root()
    script = repo_root / "vol3" / "scripts" / "fetch_vmlinux.sh"

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    apt_log = tmp_path / "apt.log"
    apt_stub = stub_dir / "apt-get"
    apt_stub.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{apt_log}"\nexit 0\n')
    apt_stub.chmod(0o755)
    py_stub = stub_dir / "python3"
    py_stub.write_text(
        '#!/usr/bin/env bash\nif [[ $# -eq 1 ]]; then echo ""; exit 0; fi\necho "Launchpad unreachable" >&2; exit 1\n'
    )
    py_stub.chmod(0o755)
    curl_stub = stub_dir / "curl"
    curl_stub.write_text("#!/usr/bin/env bash\nexit 1\n")
    curl_stub.chmod(0o755)
    tee_log = tmp_path / "tee.log"
    tee_stub = stub_dir / "tee"
    tee_stub.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{tee_log}"\ncat > /dev/null\n')
    tee_stub.chmod(0o755)

    env = {
        **dict(__import__("os").environ),
        "PATH": f"{stub_dir}:{__import__('os').environ['PATH']}",
        "LP_QUERY_ATTEMPTS": "1",
        "DOWNLOAD_ATTEMPTS": "1",
    }
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--work-dir",
            str(work_dir),
            "--distro",
            "ubuntu",
            "--version",
            "25.04",
            "--kernel-release",
            "6.14.0-36-generic",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0
    assert not tee_log.exists() or "/etc/apt" not in tee_log.read_text(), (
        f"tee was invoked targeting /etc/apt: {tee_log.read_text()}"
    )
    if apt_log.exists():
        invocations = apt_log.read_text()
        assert "install -y ubuntu-dbgsym-keyring" not in invocations
        assert "update" not in invocations
        assert "download" not in invocations


def test_nginx_listing_fallback_when_launchpad_unreachable(tmp_path):
    """When LP fails, script should scrape ddebs nginx index and pick newest match."""
    repo_root = _repo_root()
    script = repo_root / "vol3" / "scripts" / "fetch_vmlinux.sh"
    fixture = repo_root / "tests" / "python" / "fixtures" / "ddebs_index.html"

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()

    py_stub = stub_dir / "python3"
    py_stub.write_text(
        '#!/usr/bin/env bash\nif [[ $# -eq 1 ]]; then echo ""; exit 0; fi\necho "Launchpad unreachable" >&2; exit 1\n'
    )
    py_stub.chmod(0o755)

    curl_stub = stub_dir / "curl"
    curl_stub.write_text(
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '  if [[ "$arg" == *"pool/main/l/linux/"* && "$arg" != *.ddeb ]]; then\n'
        f'    cat "{fixture}"; exit 0\n'
        "  fi\n"
        "done\n"
        "exit 1\n"
    )
    curl_stub.chmod(0o755)

    wget_log = tmp_path / "wget.log"
    wget_stub = stub_dir / "wget"
    wget_stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{wget_log}"\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  if [[ "$1" == "-O" ]]; then : > "$2"; fi\n'
        "  shift\n"
        "done\n"
        "exit 0\n"
    )
    wget_stub.chmod(0o755)

    env = {
        **dict(__import__("os").environ),
        "PATH": f"{stub_dir}:{__import__('os').environ['PATH']}",
        "LP_QUERY_ATTEMPTS": "1",
        "DOWNLOAD_ATTEMPTS": "1",
    }
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--work-dir",
            str(work_dir),
            "--distro",
            "ubuntu",
            "--version",
            "25.04",
            "--kernel-release",
            "6.14.0-36-generic",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert wget_log.exists(), result.stderr
    invoked = wget_log.read_text()
    assert "linux-image-unsigned-6.14.0-36-generic-dbgsym_6.14.0-36.37_amd64.ddeb" in invoked, invoked
    assert "6.14.0-37-generic" not in invoked


def test_guest_direct_download_when_apt_fails(tmp_path):
    """When apt install fails, guest script should wget ddeb from ddebs.ubuntu.com."""
    repo_root = _repo_root()
    script = repo_root / "guest" / "scripts" / "build_vol3_symbol.sh"
    fixture = repo_root / "tests" / "python" / "fixtures" / "ddebs_index.html"

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()

    apt_stub = stub_dir / "apt-get"
    apt_stub.write_text('#!/usr/bin/env bash\ncase "$1" in install) exit 100;; *) exit 0;; esac\n')
    apt_stub.chmod(0o755)

    dpkg_stub = stub_dir / "dpkg"
    dpkg_stub.write_text("#!/usr/bin/env bash\nexit 1\n")  # keyring not installed

    dpkg_stub.chmod(0o755)

    curl_stub = stub_dir / "curl"
    curl_stub.write_text(
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '  if [[ "$arg" == *"pool/main/l/linux/"* && "$arg" != *.ddeb ]]; then\n'
        f'    cat "{fixture}"; exit 0\n'
        "  fi\n"
        "done\n"
        "exit 1\n"
    )
    curl_stub.chmod(0o755)

    wget_log = tmp_path / "wget.log"
    wget_stub = stub_dir / "wget"
    wget_stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{wget_log}"\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  if [[ "$1" == "-O" ]]; then : > "$2"; fi\n'
        "  shift\n"
        "done\n"
        "exit 0\n"
    )
    wget_stub.chmod(0o755)

    dpkg_deb_stub = stub_dir / "dpkg-deb"
    dpkg_deb_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    dpkg_deb_stub.chmod(0o755)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    fake_d2j = work_dir / "dwarf2json"
    fake_d2j.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_d2j.chmod(0o755)

    env = {
        **dict(__import__("os").environ),
        "PATH": f"{stub_dir}:{__import__('os').environ['PATH']}",
        "KERNEL_RELEASE": "6.14.0-36-generic",
        "VOL3_ALLOW_GUEST_PACKAGE_INSTALL": "1",
    }
    result = subprocess.run(
        ["bash", str(script), str(work_dir)],
        capture_output=True,
        text=True,
        env=env,
    )
    # Script will exit non-zero (no real vmlinux) but wget must have been invoked
    assert wget_log.exists(), result.stderr
    assert "linux-image-unsigned-6.14.0-36-generic-dbgsym" in wget_log.read_text()


def test_guest_honors_vmlinux_override(tmp_path):
    """When VMLINUX_OVERRIDE is set and exists, guest script skips discovery."""
    repo_root = _repo_root()
    script = repo_root / "guest" / "scripts" / "build_vol3_symbol.sh"
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    fake_vmlinux = tmp_path / "fake-vmlinux"
    fake_vmlinux.write_bytes(b"\x7fELF stub")
    fake_d2j = work_dir / "dwarf2json"
    fake_d2j.write_text('#!/usr/bin/env bash\necho "{}"\n')
    fake_d2j.chmod(0o755)

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    for blocker in ("apt-get", "dnf", "yum"):
        b = stub_dir / blocker
        b.write_text('#!/usr/bin/env bash\necho "SHOULD NOT BE CALLED: $0 $@" >&2\nexit 99\n')
        b.chmod(0o755)

    env = {
        **dict(__import__("os").environ),
        "PATH": f"{stub_dir}:{__import__('os').environ['PATH']}",
        "KERNEL_RELEASE": "6.14.0-36-generic",
        "VMLINUX_OVERRIDE": str(fake_vmlinux),
    }
    result = subprocess.run(
        ["bash", str(script), str(work_dir)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "vol3_symbol_remote_path=" in result.stdout
    assert "Using vmlinux override" in result.stderr


def test_deprecated_vol3_case_workflow_scripts_are_not_expected():
    repo_root = _repo_root()
    scripts_dir = repo_root / "vol3" / "scripts"
    deprecated_scripts = [
        "run_from_config.sh",
        "run_volatility.sh",
        "case_init.sh",
        "export_guest_metadata.sh",
        "export_memory_artifact.sh",
        "validate_dump.sh",
        "build_linux_symbols.sh",
    ]

    assert all(not (scripts_dir / name).exists() for name in deprecated_scripts)


def test_guest_btf_only_input_is_not_sent_to_dwarf2json(tmp_path):
    """A raw BTF blob is not a supported dwarf2json input."""
    repo_root = _repo_root()
    script = repo_root / "guest" / "scripts" / "build_vol3_symbol.sh"
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    invocation_log = tmp_path / "dwarf2json-invocation.log"
    fake_d2j = work_dir / "dwarf2json"
    fake_d2j.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" > "$DWARF2JSON_INVOCATION_LOG"\n',
        encoding="utf-8",
    )
    fake_d2j.chmod(0o755)

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()

    fake_btf = tmp_path / "fake_btf_vmlinux"
    fake_btf.write_bytes(b"\x9f\xeb\x01\x00")

    for pm in ("apt-get", "dnf", "yum"):
        blocker = stub_dir / pm
        blocker.write_text(
            '#!/usr/bin/env bash\necho "BLOCKED: $0 $@" >&2\nexit 99\n',
            encoding="utf-8",
        )
        blocker.chmod(0o755)

    import os

    env = {
        **dict(os.environ),
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "KERNEL_RELEASE": "6.1.0-18-amd64",
        "VMLINUX_OVERRIDE": str(fake_btf),
        "DWARF2JSON_INVOCATION_LOG": str(invocation_log),
        "VOL3_ALLOW_GUEST_PACKAGE_INSTALL": "0",
    }
    result = subprocess.run(
        ["bash", str(script), str(work_dir)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 1, result.stderr
    assert "vmlinux input is not an ELF file" in result.stderr
    assert not invocation_log.exists(), "BTF-only input reached dwarf2json"


def test_fetch_vmlinux_debian_uses_hash_not_timestamp():
    """_fetch_debian must use snapshot.debian.org/file/<hash>, not a hardcoded timestamp."""
    repo_root = _repo_root()
    script = (repo_root / "vol3" / "scripts" / "fetch_vmlinux.sh").read_text(encoding="utf-8")

    assert "snapshot.debian.org/file/" in script
    assert "binfiles" in script
    # Hardcoded timestamps must no longer exist
    assert "20240101T000000Z" not in script
    assert "20230101T000000Z" not in script


def test_fetch_vmlinux_debuginfod_uses_guest_build_id_not_boot():
    """_debuginfod_try must use GUEST_BUILD_ID and must not execute eu-readelf on /boot."""
    repo_root = _repo_root()
    script = (repo_root / "vol3" / "scripts" / "fetch_vmlinux.sh").read_text(encoding="utf-8")

    assert "GUEST_BUILD_ID" in script
    # The old code read eu-readelf on the host's /boot — that executable line must be gone.
    assert 'eu-readelf -n "$vmlinuz"' not in script
    assert 'vmlinuz="/boot/vmlinuz-${KERNEL_RELEASE}"' not in script


def test_fetch_vmlinux_kali_parrot_dispatch():
    """fetch_vmlinux.sh must route kali/parrot to _fetch_kali, not _fetch_debian."""
    repo_root = _repo_root()
    script = (repo_root / "vol3" / "scripts" / "fetch_vmlinux.sh").read_text(encoding="utf-8")

    assert "kali | parrot" in script
    assert "_fetch_kali" in script
    # debian case must NOT include kali or parrot
    # Find the dispatch block and verify the debian line is standalone
    assert "debian)\n    _fetch_debian" in script or "debian)\n    _fetch_debian" in script.replace("\r\n", "\n")


def test_fetch_rpm_requires_kernel_qualified_vmlinux():
    text = (_repo_root() / "vol3" / "scripts" / "fetch_vmlinux.sh").read_text(encoding="utf-8")
    assert '-path "*/modules/${KERNEL_RELEASE}/vmlinux"' in text
    assert "Extracted vmlinux does not match requested kernel" in text


def test_remote_symbol_script_parses_last_occurrence_and_validates():
    script = (_repo_root() / "ops" / "host" / "build_vol3_symbol_remote.sh").read_text(encoding="utf-8")
    assert "grep '^vol3_symbol_remote_path=' | tail -n 1" in script
    assert "head -1 | cut" not in script
    assert "guest reported invalid kernel_release" in script
    assert "guest reported invalid build_id" in script
    assert 'EXPECTED_REMOTE_PATH="${REMOTE_TMP_DIR}/linux-${KERNEL_RELEASE}.json"' in script


def test_live_guest_wrapper_parses_last_occurrence():
    script = (_repo_root() / "vol3" / "scripts" / "build_symbol_from_live_guest.sh").read_text(encoding="utf-8")
    assert "head -1" not in script
    assert "tail -n 1" in script
