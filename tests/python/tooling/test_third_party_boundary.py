import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INSTALL = ROOT / "ops/deploy/install.sh"
GITIGNORE = ROOT / ".gitignore"
PRODUCTION_SOURCE = ROOT / "src" / "astinuslab"
THIRD_PARTY_NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
LICENSES = ROOT / "LICENSES"
HTMX_ASSET = ROOT / "src" / "astinuslab" / "web" / "static" / "htmx.min.js"
FONT_DIR = ROOT / "src" / "astinuslab" / "web" / "static" / "fonts"
REQUIRED_NOTICE_FILES = (
    LICENSES / "dependency-inventory.json",
    THIRD_PARTY_NOTICES,
    LICENSES / "README.md",
    LICENSES / "VSL-1.0.txt",
    LICENSES / "pflag-BSD-3-Clause.txt",
    LICENSES / "go-BSD-3-Clause.txt",
    LICENSES / "htmx-0BSD.txt",
    # OFL 1.1 makes these two a licence condition, not inventory completeness:
    # they must travel with the font files AstinusLab redistributes.
    LICENSES / "inter-OFL-1.1.txt",
    LICENSES / "jetbrains-mono-OFL-1.1.txt",
)
pytestmark = [pytest.mark.unit, pytest.mark.host]


def test_dependency_inventory_matches_locked_packages_and_artifact_digests():
    inventory = json.loads((LICENSES / "dependency-inventory.json").read_text())
    for name, expected in inventory["lockfiles"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, (
            f"Refresh the legal dependency inventory after changing {name}"
        )
    python_lock = {
        (p["name"], p["version"]): p
        for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]
        if p["name"] != "astinuslab"
    }
    npm_lock = json.loads((ROOT / "package-lock.json").read_text())["packages"]
    python_rows = [p for p in inventory["packages"] if p["ecosystem"] == "python"]
    npm_rows = [p for p in inventory["packages"] if p["ecosystem"] == "npm"]
    assert {(p["name"], p["version"]) for p in python_rows} == set(python_lock)
    assert len(python_rows) == len(python_lock)
    assert {p["lock_path"] for p in npm_rows} == set(npm_lock) - {""}
    assert len(npm_rows) == len(npm_lock) - 1
    for row in python_rows:
        locked = python_lock[row["name"], row["version"]]
        artifacts = locked.get("wheels", []) + ([locked["sdist"]] if "sdist" in locked else [])
        assert any(
            a["url"] == row["artifact_url"] and a["hash"] == "sha256:" + row["artifact_sha256"] for a in artifacts
        )
    for row in npm_rows:
        locked = npm_lock[row["lock_path"]]
        assert row["version"] == locked["version"]
        assert row["source"] == locked["resolved"]
        assert row["lock_integrity"] == locked["integrity"]


@pytest.mark.parametrize(
    "source",
    [
        "import volatility3.plugins\n",
        "import os, volatility3.plugins\n",
        "from volatility3 import framework\n",
        "marker = True; import volatility3.framework\n",
        'importlib.import_module("volatility3.plugins")\n',
        '__import__("volatility3.framework")\n',
    ],
)
def test_production_import_guard_rejects_synthetic_import_forms(tmp_path, source):
    synthetic_source = tmp_path / "synthetic.py"
    synthetic_source.write_text(source, encoding="utf-8")

    assert _production_volatility_import_violations(tmp_path) == [f"{synthetic_source}:1: {source.strip()}"]


def _is_volatility_module(name: str | None) -> bool:
    return name == "volatility3" or bool(name and name.startswith("volatility3."))


def _literal_import_target(node: ast.Call) -> str | None:
    if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
        return None
    if isinstance(node.func, ast.Name) and node.func.id == "__import__":
        return node.args[0].value
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "import_module"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "importlib"
    ):
        return node.args[0].value
    return None


def _volatility_import_violations(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    source_lines = source.splitlines()
    violation_lines: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(_is_volatility_module(alias.name) for alias in node.names):
            violation_lines.add(node.lineno)
        elif isinstance(node, ast.ImportFrom) and _is_volatility_module(node.module):
            violation_lines.add(node.lineno)
        elif isinstance(node, ast.Call) and _is_volatility_module(_literal_import_target(node)):
            violation_lines.add(node.lineno)

    try:
        display_path = path.relative_to(ROOT)
    except ValueError:
        display_path = path
    return [
        f"{display_path}:{line_number}: {source_lines[line_number - 1].strip()}"
        for line_number in sorted(violation_lines)
    ]


def _production_volatility_import_violations(source_root: Path) -> list[str]:
    return [
        violation for path in sorted(source_root.rglob("*.py")) for violation in _volatility_import_violations(path)
    ]


def test_production_source_does_not_import_volatility3():
    violations = _production_volatility_import_violations(PRODUCTION_SOURCE)

    assert not violations, "Production source must not import volatility3 in-process:\n" + "\n".join(violations)


def test_vol3_bin_contains_no_tracked_release_artifacts():
    result = subprocess.run(
        ["git", "ls-files", "--", "vol3/bin/**"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = result.stdout.splitlines()
    assert not tracked, f"Forbidden tracked path(s) below vol3/bin/: {', '.join(tracked)}"


def test_installer_permanently_excludes_vol3_bin():
    installer = INSTALL.read_text(encoding="utf-8")
    assert "--exclude='/vol3/bin/'" in installer, (
        "Forbidden installed path vol3/bin/: ops/deploy/install.sh must contain "
        "the anchored rsync exclusion --exclude='/vol3/bin/'"
    )


def test_gitignore_excludes_vol3_bin():
    sentinel = "vol3/bin/dwarf2json"
    ignored_entries = {
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "/vol3/bin/" in ignored_entries, "Forbidden release path vol3/bin/ must be root-anchored in .gitignore"

    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", sentinel],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Forbidden release path {sentinel} is not effectively ignored by .gitignore: "
        f"{result.stderr.strip() or 'git check-ignore reported it as unignored'}"
    )


def test_installer_does_not_copy_dwarf2json_sentinel(tmp_path):
    if not shutil.which("rsync"):
        pytest.skip("rsync is required to verify the installer release boundary")

    repo = tmp_path / "repo"
    install = tmp_path / "install"
    sentinel = repo / "vol3/bin/dwarf2json"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("synthetic third-party sentinel\n", encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "INSTALL_SCRIPT": str(INSTALL),
            "FIXTURE_REPO": str(repo),
            "FIXTURE_INSTALL": str(install),
        }
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$INSTALL_SCRIPT"; REPO_DIR="$FIXTURE_REPO"; INSTALL_DIR="$FIXTURE_INSTALL"; sync_source_tree',
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    installed_sentinel = install / "vol3/bin/dwarf2json"
    assert not installed_sentinel.exists(), f"Forbidden installed path: {installed_sentinel}"


@pytest.mark.parametrize("notice_path", REQUIRED_NOTICE_FILES, ids=lambda path: str(path.relative_to(ROOT)))
def test_required_notice_file_exists(notice_path):
    assert notice_path.is_file(), f"Required third-party notice file is missing: {notice_path.relative_to(ROOT)}"


# The published docs site serves its own tracked copies of these two files, and
# the docs copy is what operators actually read. Nothing keeps them in step: the
# notice tests below only ever read the root copy, so a correction applied there
# alone would leave the site asserting superseded licensing facts while every
# gate stayed green.
DUPLICATED_LEGAL_DOCS = (
    (ROOT / "LICENSE", ROOT / "docs" / "LICENSE.md"),
    (THIRD_PARTY_NOTICES, ROOT / "docs" / "THIRD_PARTY_NOTICES.md"),
)


@pytest.mark.parametrize(
    ("source", "published"),
    DUPLICATED_LEGAL_DOCS,
    ids=lambda path: str(path.relative_to(ROOT)),
)
def test_published_legal_doc_matches_its_root_original(source, published):
    assert source.is_file(), f"missing root original: {source.relative_to(ROOT)}"
    assert published.is_file(), f"missing published copy: {published.relative_to(ROOT)}"
    assert published.read_bytes() == source.read_bytes(), (
        f"{published.relative_to(ROOT)} has drifted from {source.relative_to(ROOT)}; "
        "a licensing correction must land on both copies"
    )


def test_notice_inventory_covers_external_tool_boundary_and_provenance():
    notices = THIRD_PARTY_NOTICES.read_text(encoding="utf-8")
    required_fragments = (
        "AstinusLab-owned source",
        "Volatility 3 Framework",
        "dwarf2json",
        "pflag",
        "Go runtime",
        "Windows symbol packages/catalog content",
        "operator-supplied",
        "not shipped",
        "https://www.volatilityfoundation.org/license/vsl-v1.0",
        "https://github.com/volatilityfoundation/volatility3",
        "https://github.com/volatilityfoundation/dwarf2json",
        "https://github.com/volatilityfoundation/dwarf2json/releases/download/v0.9.0/dwarf2json-linux-amd64",
        "9f14607e0d339d463ea725fbd5c08aa7b7d40f75",
        "e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b",
        # The one third-party payload AstinusLab ships; its provenance block is
        # pinned to the shipped bytes by test_shipped_htmx_asset_matches_its_notice.
        "htmx",
        "src/astinuslab/web/static/htmx.min.js",
        "https://github.com/bigskysoftware/htmx",
        "https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.4/dist/htmx.min.js",
        "LICENSES/htmx-0BSD.txt",
        "Zero-Clause BSD",
        # The two self-hosted type families; their bytes are pinned by
        # test_shipped_font_assets_match_their_notice.
        "SIL Open Font License 1.1",
        "src/astinuslab/web/static/fonts/InterVariable.woff2",
        "https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip",
        "LICENSES/inter-OFL-1.1.txt",
        "https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip",
        "LICENSES/jetbrains-mono-OFL-1.1.txt",
        "Reserved Font Name",
    )
    missing = [fragment for fragment in required_fragments if fragment not in notices]
    assert not missing, f"THIRD_PARTY_NOTICES.md is missing required boundary/provenance text: {missing}"
    assert "does not mean" in notices and "source archive" in notices
    assert "automatic allow-list" in notices


def test_audited_dwarf2json_metadata_matches_notice_inventory():
    from astinuslab.forensics.tool_metadata import DWARF2JSON_AMD64

    notices = THIRD_PARTY_NOTICES.read_text(encoding="utf-8")
    for value in (
        DWARF2JSON_AMD64.version,
        DWARF2JSON_AMD64.commit,
        DWARF2JSON_AMD64.download_url,
        DWARF2JSON_AMD64.sha256,
    ):
        assert value in notices


def test_shipped_htmx_asset_matches_its_notice():
    """Pin every htmx fact in THIRD_PARTY_NOTICES.md to the bytes actually shipped.

    `src/astinuslab/web/static/htmx.min.js` is the only third-party payload in the AstinusLab
    tree, and the notice asserts its SHA-256, byte length, upstream tag, and embedded
    version string.  The expected values are parsed OUT of the notice rather than
    duplicated here, so dropping in a new htmx build without updating the notice —
    or editing the notice without replacing the file — fails in either direction.
    """
    assert HTMX_ASSET.is_file(), "Shipped htmx asset is missing"
    notices = THIRD_PARTY_NOTICES.read_text(encoding="utf-8")

    def _claim(pattern: str, label: str) -> str:
        match = re.search(pattern, notices)
        assert match is not None, f"THIRD_PARTY_NOTICES.md no longer states the shipped htmx {label}"
        return match.group(1)

    claimed_sha256 = _claim(r"shipped file SHA-256:\s*`([0-9a-f]{64})`", "SHA-256")
    claimed_size = int(_claim(r"shipped file size:\s*([0-9]+) bytes", "byte length"))
    claimed_version = _claim(r'embedded version string in the shipped file:\s*`version:"([^"]+)"`', "version string")
    claimed_tag = _claim(r"upstream tag:\s*`v([0-9][^`]*)`", "upstream tag")

    payload = HTMX_ASSET.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == claimed_sha256, (
        "Shipped htmx SHA-256 disagrees with THIRD_PARTY_NOTICES.md"
    )
    assert len(payload) == claimed_size, "Shipped htmx byte length disagrees with THIRD_PARTY_NOTICES.md"
    assert f'version:"{claimed_version}"'.encode() in payload, (
        "Shipped htmx does not embed the version string THIRD_PARTY_NOTICES.md claims"
    )
    # The upstream tag and the embedded version must describe the same release.
    assert claimed_tag == claimed_version, "THIRD_PARTY_NOTICES.md htmx upstream tag and version string disagree"
    # The retained upstream licence text must be the one the inventory records.
    inventory = (LICENSES / "README.md").read_text(encoding="utf-8")
    assert f"tag `v{claimed_version}`" in inventory
    assert f"htmx/v{claimed_version}/dist/htmx.min.js" in notices


def test_shipped_font_assets_match_their_notice():
    """Pin every shipped font file to the row THIRD_PARTY_NOTICES.md states for it.

    The expected SHA-256 and byte length are parsed OUT of the notice table, so
    replacing a font without updating the notice — or editing the notice without
    replacing the file — fails in either direction, exactly as the htmx pin does.
    The set of rows must also match the set of shipped files, so adding a face
    without a notice row (or removing one and leaving its row) fails too.
    """
    notices = THIRD_PARTY_NOTICES.read_text(encoding="utf-8")
    claimed = {
        match.group("name"): (match.group("sha256"), int(match.group("size")))
        for match in re.finditer(
            r"^\| `(?P<name>[A-Za-z0-9\-]+\.woff2)` \| (?P<size>[0-9]+) \| `(?P<sha256>[0-9a-f]{64})` \|",
            notices,
            re.MULTILINE,
        )
    }
    assert claimed, "THIRD_PARTY_NOTICES.md no longer states any shipped font provenance row"

    shipped = {path.name for path in FONT_DIR.glob("*.woff2")}
    assert shipped == set(claimed), (
        "Shipped fonts and their THIRD_PARTY_NOTICES.md rows disagree; "
        f"only in payload: {sorted(shipped - set(claimed))}; only in notice: {sorted(set(claimed) - shipped)}"
    )

    for name, (expected_sha256, expected_size) in sorted(claimed.items()):
        payload = (FONT_DIR / name).read_bytes()
        assert len(payload) == expected_size, f"{name} byte length disagrees with THIRD_PARTY_NOTICES.md"
        assert hashlib.sha256(payload).hexdigest() == expected_sha256, (
            f"{name} SHA-256 disagrees with THIRD_PARTY_NOTICES.md"
        )
        # A woff2 payload always opens with the wOF2 signature.
        assert payload[:4] == b"wOF2", f"{name} is not a woff2 payload"


def test_shipped_fonts_are_declared_as_package_data():
    """A font missing from package-data would 404 in an installed release only."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"static/fonts/*.woff2"' in pyproject, (
        "Shipped fonts must be declared in the astinuslab.web setuptools package data"
    )


def test_font_faces_reference_only_shipped_font_files():
    """Every @font-face src must resolve to a file that actually ships."""
    tokens = (ROOT / "src" / "astinuslab" / "web" / "static" / "css" / "tokens.css").read_text(encoding="utf-8")
    referenced = set(re.findall(r'url\("/static/fonts/([^"]+)"\)', tokens))
    assert referenced, "tokens.css no longer declares any self-hosted @font-face source"
    missing = sorted(name for name in referenced if not (FONT_DIR / name).is_file())
    assert not missing, f"@font-face references font files that are not shipped: {missing}"


def test_license_inventory_records_authoritative_sources_and_exact_hashes():
    inventory = (LICENSES / "README.md").read_text(encoding="utf-8")
    expected = {
        "VSL-1.0.txt": (
            "https://raw.githubusercontent.com/volatilityfoundation/dwarf2json/"
            "9f14607e0d339d463ea725fbd5c08aa7b7d40f75/LICENSE.txt",
            "94e8e631513d38746775e31e7ad84065d009a57cd80d5d4c4e7c7182373e7cbe",
        ),
        "pflag-BSD-3-Clause.txt": (
            "https://raw.githubusercontent.com/spf13/pflag/v1.0.5/LICENSE",
            "b8514c577c1c4b46cee454d5a882b15fa411e72c5bd7f801f241591789fce61a",
        ),
        "go-BSD-3-Clause.txt": (
            "https://raw.githubusercontent.com/golang/go/go1.18.10/LICENSE",
            "2d36597f7117c38b006835ae7f537487207d8ec407aa9d9980794b2030cbc067",
        ),
        "htmx-0BSD.txt": (
            "https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.4/LICENSE",
            "d3d2456f76414f2456104660ebd65aff1c04cd7966b942bdabd63f3cdb316a38",
        ),
        "inter-OFL-1.1.txt": (
            "https://raw.githubusercontent.com/rsms/inter/v4.1/LICENSE.txt",
            "262481e844521b326f5ecd053e59b98c8b2da78c8ee1bdbb6e8174305e54935a",
        ),
        "jetbrains-mono-OFL-1.1.txt": (
            "https://raw.githubusercontent.com/JetBrains/JetBrainsMono/v2.304/OFL.txt",
            "30f0c136e3c88e422d0791acd97238870f9054a9729bc34cf2ff0d4ed8cac4ad",
        ),
    }

    assert "Retrieved: 2026-08-04" in inventory
    assert "https://raw.githubusercontent.com/volatilityfoundation/volatility3/v2.28.0/LICENSE.txt" in inventory
    assert "6e905a859f6d5ebf23d4b7cdfb8ac425c4ec5dd02b3b274901a967f3e452eb37" in inventory
    assert "dwarf2json/9f14607e0d339d463ea725fbd5c08aa7b7d40f75/LICENSE.txt" in inventory
    for filename, (source_url, expected_sha256) in expected.items():
        assert source_url in inventory
        assert expected_sha256 in inventory
        payload = (LICENSES / filename).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected_sha256


def test_installer_includes_notice_inventory(tmp_path):
    if not shutil.which("rsync"):
        pytest.skip("rsync is required to verify the installer notice boundary")

    repo = tmp_path / "repo"
    install = tmp_path / "install"
    notice_paths = [path.relative_to(ROOT) for path in REQUIRED_NOTICE_FILES]
    for notice_path in notice_paths:
        source = repo / notice_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"synthetic notice: {notice_path}\n", encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "INSTALL_SCRIPT": str(INSTALL),
            "FIXTURE_REPO": str(repo),
            "FIXTURE_INSTALL": str(install),
        }
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$INSTALL_SCRIPT"; REPO_DIR="$FIXTURE_REPO"; INSTALL_DIR="$FIXTURE_INSTALL"; sync_source_tree',
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert all((install / notice_path).is_file() for notice_path in notice_paths)


def test_source_archive_rules_include_notice_inventory(tmp_path):
    notice_paths = [path.relative_to(ROOT) for path in REQUIRED_NOTICE_FILES]
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--", *map(str, notice_paths)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert set(tracked.stdout.splitlines()) == {str(path) for path in notice_paths}, (
        "Required notices must be tracked before source-archive verification"
    )

    attributes = subprocess.run(
        ["git", "check-attr", "export-ignore", "--", *map(str, notice_paths)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    excluded = [line for line in attributes.stdout.splitlines() if not line.endswith(": unspecified")]
    assert not excluded, "Required notices must be included by git archive rules:\n" + "\n".join(excluded)

    archive_repo = tmp_path / "archive-repo"
    archive_repo.mkdir()
    for notice_path in notice_paths:
        destination = archive_repo / notice_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / notice_path, destination)
    attributes_path = ROOT / ".gitattributes"
    archive_inputs = [str(path) for path in notice_paths]
    if attributes_path.is_file():
        shutil.copy2(attributes_path, archive_repo / ".gitattributes")
        archive_inputs.append(".gitattributes")

    git_commands = (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "AstinusLab archive test"],
        ["git", "config", "user.email", "archive-test@example.invalid"],
        ["git", "add", "--", *archive_inputs],
        ["git", "commit", "-qm", "archive fixture"],
    )
    for command in git_commands:
        subprocess.run(command, cwd=archive_repo, capture_output=True, text=True, check=True)

    archive_path = tmp_path / "source.tar"
    subprocess.run(
        ["git", "archive", "--format=tar", f"--output={archive_path}", "HEAD"],
        cwd=archive_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    with tarfile.open(archive_path) as archive:
        archived_paths = set(archive.getnames())
    assert {str(path) for path in notice_paths} <= archived_paths


LOCAL_AGENT_OUTPUT = (".agents", ".claude", ".hermes", ".superpowers")


def _archive_tree_differences(tracked_paths, archived_paths):
    """Return every tracked omission and unexpected archive member."""
    tracked = set(tracked_paths)
    archived = set(archived_paths)
    return tracked - archived, archived - tracked


def test_archive_tree_comparison_reports_an_omitted_arbitrary_tracked_path():
    tracked = {"LICENSE", "THIRD_PARTY_NOTICES.md", "ops/deploy/install.sh", "internal/source.py"}
    archived = {"LICENSE", "THIRD_PARTY_NOTICES.md", "ops/deploy/install.sh"}

    missing, unexpected = _archive_tree_differences(tracked, archived)

    assert missing == {"internal/source.py"}
    assert not unexpected


def test_local_agent_output_is_untracked_and_cannot_reach_the_release_archive(tmp_path):
    """Local execution output is absent from tracked source, not archive-filtered.

    A clean clone has no .agents/.claude/.hermes/.superpowers tree. Local copies may remain in a
    contributor's ignored working directory after ``git rm --cached``. Because
    ``git archive`` only sees tracked files, untracking is the stronger and less
    fragile release boundary and no export-ignore exception is needed.
    """
    attributes = ROOT / ".gitattributes"
    assert attributes.is_file(), ".gitattributes must preserve source-archive policy"

    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", *LOCAL_AGENT_OUTPUT],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    assert not tracked, f"local agent output remains tracked: {tracked.split(bytes([0]))}"

    attribute_text = attributes.read_text(encoding="utf-8")
    for name in LOCAL_AGENT_OUTPUT:
        assert f"/{name} export-ignore" not in attribute_text, f"stale archive exception remains for {name}"

    archive_path = tmp_path / "source.tar"
    subprocess.run(
        ["git", "archive", "--format=tar", f"--output={archive_path}", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    with tarfile.open(archive_path) as archive:
        members = [member.name for member in archive.getmembers() if member.isfile() or member.issym()]

    tracked_tree = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    missing, unexpected = _archive_tree_differences(tracked_tree, members)
    assert not missing, f"tracked paths omitted from source archive: {sorted(missing)[:10]}"
    assert not unexpected, f"untracked paths reached source archive: {sorted(unexpected)[:10]}"

    attributes = subprocess.run(
        ["git", "check-attr", "export-ignore", "--", *tracked_tree],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    export_ignored = [line for line in attributes if not line.endswith(": unspecified")]
    assert not export_ignored, "tracked paths must not use export-ignore:\n" + "\n".join(export_ignored)

    leaked = [name for name in members if name.split("/")[0] in LOCAL_AGENT_OUTPUT]
    assert not leaked, f"agent tooling reached the release archive: {leaked[:5]}"

    for required in ("LICENSE", "THIRD_PARTY_NOTICES.md", "ops/deploy/install.sh"):
        assert required in members, f"required archive member missing: {required}"
    assert any(name.startswith("tests/python/") for name in members), "archive must retain tests/python"
    assert any(name.startswith("docs/") for name in members), "archive must retain docs"
