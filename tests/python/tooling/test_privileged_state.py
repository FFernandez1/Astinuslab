"""Custody contract for the checks `ops/deploy/privileged_state.py` makes.

These two guards used to be `python3 - <<'PY'` heredocs, and
`split_identity_marker_is_valid` was three byte-identical copies across
`install.sh`, `configure-privileged-roots.sh` and `uninstall.sh`, held in step by
a test that compared the three texts. Comparing texts proved they agreed; it
never proved what they agreed on. These cases do.

Both guards decide whether privileged state may be believed or acted on, and
both answer "no" for anything an unprivileged user could have arranged. That
means the refusals are the contract, so each one gets a case. `install.sh` and
`uninstall.sh` fail closed on a refusal -- uninstall refuses to reach
`rm -rf $INSTALL_DIR` at all -- so a guard that wrongly says yes is the defect
worth catching.

Run under `unshare --map-root-user`, because every condition is about uid 0
ownership and a test user cannot create a root-owned file otherwise.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PRIVILEGED_STATE = ROOT / "ops/deploy/privileged_state.py"
pytestmark = [pytest.mark.unit, pytest.mark.host]

VALID_MARKER = (
    b"mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n"
)


def _run_mapped_root(*args: str) -> subprocess.CompletedProcess[str]:
    """Run one subcommand in a namespace where the caller is uid 0."""
    if shutil.which("unshare") is None:
        pytest.skip("unshare is required to stage root-owned privileged state")
    return subprocess.run(
        ["unshare", "--map-root-user", "--map-auto", "python3", str(PRIVILEGED_STATE), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _run_on_private_tmpfs(script: str) -> subprocess.CompletedProcess[str]:
    """Run *script* with a root-owned, mode-755 tmpfs at /mnt.

    `install-dir-authority` requires every named ancestor to be root:root and
    not group/other-writable, and pytest's `tmp_path` lives under /tmp, which is
    1777. A private tmpfs in its own mount namespace is the only way to stage a
    chain that can legitimately be accepted without being real root. `/` itself
    is never ownership-checked by the guard, which is why this works at all:
    inside a user namespace `/` reads as nobody.
    """
    if shutil.which("unshare") is None:
        pytest.skip("unshare is required to stage root-owned privileged state")
    result = subprocess.run(
        [
            "unshare",
            "--map-root-user",
            "--map-auto",
            "--mount",
            "sh",
            "-c",
            f"mount -t tmpfs -o mode=755 none /mnt || exit 99\n{script}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 99:
        pytest.skip("this environment cannot mount a private tmpfs")
    return result


def _marker(tmp_path: Path, content: bytes = VALID_MARKER, *, mode: int = 0o600) -> Path:
    path = tmp_path / "runtime-identities"
    path.write_bytes(content)
    path.chmod(mode)
    return path


def test_the_exact_marker_bytes_are_believed(tmp_path):
    marker = _marker(tmp_path)

    result = _run_mapped_root("split-identity-marker", str(marker))

    assert result.returncode == 0, result.stderr


def test_an_absent_marker_reads_as_not_split(tmp_path):
    result = _run_mapped_root("split-identity-marker", str(tmp_path / "absent"))

    assert result.returncode == 1


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(VALID_MARKER + b"extra=1\n", id="trailing-line"),
        pytest.param(VALID_MARKER.replace(b"mode=split", b"mode=single"), id="different-mode"),
        pytest.param(VALID_MARKER.replace(b"runner=astinuslab-runner\n", b""), id="missing-line"),
        pytest.param(VALID_MARKER.rstrip(b"\n"), id="no-trailing-newline"),
        pytest.param(b"", id="empty"),
    ],
)
def test_anything_but_the_exact_bytes_reads_as_not_split(tmp_path, content):
    """A near-miss must not be believed. The marker decides whether uninstall
    offers to delete three system accounts or one, so a partial match is the
    dangerous case, not the obviously-wrong one."""
    marker = _marker(tmp_path, content)

    result = _run_mapped_root("split-identity-marker", str(marker))

    assert result.returncode == 1


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_a_group_or_other_writable_marker_is_refused(tmp_path, mode):
    """Correct bytes are not enough: anyone who can rewrite the file could have
    written those bytes, so writability by anyone but the owner refuses it."""
    marker = _marker(tmp_path, mode=mode)

    result = _run_mapped_root("split-identity-marker", str(marker))

    assert result.returncode == 1


def test_a_symlink_to_a_valid_marker_is_refused(tmp_path):
    real = _marker(tmp_path)
    link = tmp_path / "link-to-marker"
    link.symlink_to(real)

    result = _run_mapped_root("split-identity-marker", str(link))

    assert result.returncode == 1, "O_NOFOLLOW must refuse the link even though the target is valid"


def test_a_directory_is_not_a_marker(tmp_path):
    directory = tmp_path / "runtime-identities"
    directory.mkdir()

    result = _run_mapped_root("split-identity-marker", str(directory))

    assert result.returncode == 1


def test_a_root_owned_chain_is_accepted_as_an_install_root():
    result = _run_on_private_tmpfs(
        f"mkdir -m 755 /mnt/astinuslab && python3 {PRIVILEGED_STATE} install-dir-authority /mnt/astinuslab"
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_missing_leaf_is_accepted_because_the_installer_creates_it():
    """The walk stops at the first component that does not exist. Uninstall runs
    this before it knows whether the root survived, so an absent leaf under a
    root-owned parent is not a custody failure."""
    result = _run_on_private_tmpfs(f"python3 {PRIVILEGED_STATE} install-dir-authority /mnt/never-created")

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("path_text", ["/opt/./astinuslab", "/opt/../opt/astinuslab", "/opt/astinuslab/.."])
def test_dot_components_are_refused_before_any_walk(path_text):
    """`..` would let a caller name one directory and have the walk prove another."""
    result = _run_mapped_root("install-dir-authority", path_text)

    assert result.returncode != 0
    assert "dot path components" in result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["775", "757", "777"])
def test_a_group_or_other_writable_ancestor_is_refused(mode):
    """`rm -rf` on the leaf is only safe if nobody but root could have arranged
    what the path resolves to, and a writable ancestor is exactly that."""
    result = _run_on_private_tmpfs(
        f"mkdir -m {mode} /mnt/opt && mkdir -m 755 /mnt/opt/astinuslab && "
        f"python3 {PRIVILEGED_STATE} install-dir-authority /mnt/opt/astinuslab"
    )

    assert result.returncode != 0
    assert "root:root" in result.stdout + result.stderr


def test_a_writable_leaf_is_refused_too():
    """The leaf is checked on the same terms as its ancestors. It is the
    directory that gets removed, so a mode anyone could have set on it is the
    one that matters most."""
    result = _run_on_private_tmpfs(
        f"mkdir -m 777 /mnt/astinuslab && python3 {PRIVILEGED_STATE} install-dir-authority /mnt/astinuslab"
    )

    assert result.returncode != 0
    assert "root:root" in result.stdout + result.stderr


def test_a_symlinked_ancestor_is_refused():
    result = _run_on_private_tmpfs(
        "mkdir -m 755 /mnt/real && mkdir -m 755 /mnt/real/astinuslab && ln -s /mnt/real /mnt/opt && "
        f"python3 {PRIVILEGED_STATE} install-dir-authority /mnt/opt/astinuslab"
    )

    assert result.returncode != 0
    assert "non-symlink directory" in result.stdout + result.stderr


def test_a_non_directory_ancestor_is_refused():
    result = _run_on_private_tmpfs(
        "printf 'not a directory\\n' > /mnt/opt && chmod 644 /mnt/opt && "
        f"python3 {PRIVILEGED_STATE} install-dir-authority /mnt/opt/astinuslab"
    )

    assert result.returncode != 0
    assert "non-symlink directory" in result.stdout + result.stderr


def test_an_ancestor_owned_by_someone_else_is_refused():
    """`--map-auto` maps uids other than the caller's to distinct unprivileged
    ones, so a chown inside the namespace produces an ancestor that genuinely is
    not root-owned."""
    result = _run_on_private_tmpfs(
        "mkdir -m 755 /mnt/opt && mkdir -m 755 /mnt/opt/astinuslab && chown 1 /mnt/opt && "
        f"python3 {PRIVILEGED_STATE} install-dir-authority /mnt/opt/astinuslab"
    )

    assert result.returncode != 0
    assert "root:root" in result.stdout + result.stderr


def test_the_usage_message_names_every_subcommand():
    result = subprocess.run(["python3", str(PRIVILEGED_STATE)], capture_output=True, text=True, check=False)

    assert result.returncode != 0
    message = result.stdout + result.stderr
    for subcommand in (
        "install-dir-authority",
        "split-identity-marker",
        "remove-privileged-helpers",
        "clean-legacy-vol3-bin",
    ):
        assert subcommand in message


def test_every_shell_caller_asks_for_a_subcommand_this_module_serves():
    """A typo in a call site would be a silent no-op at uninstall time, because
    the usage message exits non-zero and the caller reports its own failure
    instead. Pin the four spellings against the four the module accepts."""
    served = {
        "install-dir-authority",
        "split-identity-marker",
        "remove-privileged-helpers",
        "clean-legacy-vol3-bin",
    }
    requested = set()
    for name in ("install.sh", "uninstall.sh", "configure-privileged-roots.sh"):
        text = (ROOT / "ops/deploy" / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "privileged_state.py" not in line or "for module in" in line:
                continue
            fields = line.split()
            index = next(i for i, field in enumerate(fields) if "privileged_state.py" in field)
            requested.add(fields[index + 1].strip('"'))

    assert requested, "no shell caller invokes the module"
    assert requested <= served, f"unserved subcommand requested: {sorted(requested - served)}"


def test_the_module_needs_no_import_from_the_repository():
    """It is invoked as a subprocess by root, from the deploy directory, with no
    PYTHONPATH. An import of anything in this project would break that."""
    result = subprocess.run(
        ["python3", "-c", f"compile(open({str(PRIVILEGED_STATE)!r}).read(), 'privileged_state.py', 'exec')"],
        capture_output=True,
        text=True,
        check=False,
        cwd=os.sep,
    )

    assert result.returncode == 0, result.stderr
    imported = {
        node.module.split(".")[0] if isinstance(node, ast.ImportFrom) and node.module else None
        for node in ast.walk(ast.parse(PRIVILEGED_STATE.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(PRIVILEGED_STATE.read_text(encoding="utf-8")))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported <= {"__future__", "errno", "os", "stat", "sys"}, f"non-stdlib import: {imported}"
