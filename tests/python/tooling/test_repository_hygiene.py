"""Repository boundary checks for generated, runtime, and operator-owned files."""

import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
GITIGNORE = ROOT / ".gitignore"
GENERATED_RUNTIME_DOC = ROOT / "docs/development/GENERATED_RUNTIME_FILES.md"
MKDOCS_CONFIG = ROOT / "docs/mkdocs.yml"
pytestmark = [pytest.mark.unit]

_CACHE_OR_ENV_DIRS = {
    ".hypothesis",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}
_GENERATED_DIRS = {
    "build",
    "dist",
    "graphify-out",
    "htmlcov",
    "playwright-report",
    "site",
    "test-results",
}
_DATABASE_OR_RUNTIME_SUFFIXES = {
    ".db",
    ".dump",
    ".log",
    ".sqlite",
    ".sqlite3",
}
_VM_IMAGE_SUFFIXES = {".img", ".qcow2", ".raw", ".vdi", ".vhd", ".vhdx", ".vmdk"}
_MEMORY_CAPTURE_SUFFIXES = {".core", ".dmp", ".lime", ".mem", ".vmem"}
_CREDENTIAL_FILENAMES = {
    ".env",
    "credentials.json",
    "credentials.toml",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "settings.local.json",
}
_CREDENTIAL_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
_AGENT_REPORT = re.compile(r"(?:^|/)(?:task-[0-9]+-report|agent-report)(?:\.[^/]+)?$")
_ROOT_PLAN_CONTRACTS: frozenset[str] = frozenset()
_TRANSITIONAL_TRACKED_IGNORED: set[str] = set()
_INTERNAL_RECORD_ROOTS = ("docs/plans/", "docs/remediation/", "docs/audits/")
_MOVED_OLD_PATHS = (
    "plans/2026-08-14-volatility-user-supplied-tools.md",
    "plans/2026-08-15-post-review-remediation-and-release-gates.md",
    "plans/pendingimplementation/notes/PLAYGROUND-i18n-followup.md",
    "docs/superpowers/plans/2026-07-26-windows-support-parked.md",
    "docs/superpowers/plans/2026-07-29-windows-support-resume.md",
    "docs/superpowers/plans/2026-08-25-guided-install-and-volatility-onboarding.md",
    "docs/superpowers/specs/2026-07-10-review-prompt-suite-hardening-design.md",
    "docs/superpowers/specs/2026-07-15-launcher-nonzero-cleanup-design.md",
    "docs/superpowers/specs/2026-08-24-fake-libvirt-network-lifecycle-design.md",
    "docs/superpowers/specs/2026-08-25-guided-install-and-volatility-onboarding-design.md",
)
_STALE_HERMES_PLAN_PREFIX = ".hermes" + "/plans/"
_MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_MARKDOWN_REFERENCE_TARGET = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
_FENCED_CODE = re.compile(r"^\s*(```|~~~).*?^\s*\1\s*$", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return sorted(path.decode() for path in result.stdout.split(b"\0") if path)


def _check_ignored(relative: str, *, git_config: tuple[str, ...] = ()) -> bool:
    result = subprocess.run(
        ["git", *git_config, "check-ignore", "-v", "--no-index", "--", relative],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode in {0, 1}, result.stderr
    if result.returncode == 1:
        return False
    match = re.fullmatch(r"(.+?):\d+:(.*?)\t.*\n?", result.stdout)
    assert match, f"could not parse git check-ignore output: {result.stdout!r}"
    source, pattern = match.groups()
    return (ROOT / source).resolve() == GITIGNORE.resolve() and not pattern.startswith("!")


def _tracked_ignored_files(*, git_config: tuple[str, ...] = ()) -> set[str]:
    result = subprocess.run(
        ["git", *git_config, "ls-files", "-ci", "--exclude-from=.gitignore", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return {path.decode() for path in result.stdout.split(b"\0") if path}


def _tracked_text(relative: str) -> str | None:
    path = ROOT / relative
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _published_doc_sources() -> list[Path]:
    return sorted(
        path
        for path in (ROOT / "docs").rglob("*.md")
        if path.relative_to(ROOT / "docs").parts[0] not in {"plans", "superpowers"}
    )


def _prohibited_reason(relative: str) -> str | None:
    path = PurePosixPath(relative)
    parts = set(path.parts)
    name = path.name
    suffix = path.suffix.lower()

    if parts & _CACHE_OR_ENV_DIRS:
        return "cache or virtual environment"
    if suffix in {".pyc", ".pyd", ".pyo"}:
        return "Python bytecode cache"
    if parts & _GENERATED_DIRS or any(part.endswith(".egg-info") for part in path.parts):
        return "generated build, package, docs, test, or analysis output"
    if name == ".coverage" or name.startswith(".coverage.") or name in {"coverage.xml", "coverage.json"}:
        return "coverage output"
    if suffix in _DATABASE_OR_RUNTIME_SUFFIXES or name.endswith(
        (
            ".db-wal",
            ".db-shm",
            ".sqlite-wal",
            ".sqlite-shm",
            ".sqlite3-wal",
            ".sqlite3-shm",
            ".db-journal",
            ".sqlite-journal",
            ".sqlite3-journal",
        )
    ):
        return "database, log, or dump"

    if suffix in _VM_IMAGE_SUFFIXES:
        fixture = relative.startswith("tests/browser/fixtures/images/") and (ROOT / relative).stat().st_size == 0
        if not fixture:
            return "non-fixture VM image"
    if suffix == ".iso":
        return "installation media"
    if suffix in _MEMORY_CAPTURE_SUFFIXES:
        return "memory capture"
    if relative.startswith("vol3/symbols/") and name != ".gitkeep":
        return "generated Volatility symbol"

    if relative == "astinuslab.toml":
        return "live operator configuration"
    if relative.startswith("config/catalog/") and suffix in {".yaml", ".yml"} and ".example." not in name:
        return "live operator catalog without safe example classification"
    safe_env_example = name == ".env.example" or name.endswith(".example.env")
    if (name == ".env" or name.startswith(".env.") or name.endswith(".env")) and not safe_env_example:
        return "live environment configuration"
    safe_credential_example = name.startswith("credentials.example.")
    if (
        name in _CREDENTIAL_FILENAMES
        or (name.startswith("credentials.") and not safe_credential_example)
        or ".local." in name
        or suffix in _CREDENTIAL_SUFFIXES
    ):
        return "credential-bearing local settings"

    if path.parts and path.parts[0] in {
        ".agents",
        ".antigravitycli",
        ".claude",
        ".hermes",
        ".superpowers",
        "review",
        "reviewfix",
        "reviews",
    }:
        return "local agent state"
    if _AGENT_REPORT.search(relative):
        return "agent execution report"
    if path.parts and path.parts[0] in {"release-evidence", "reviews"}:
        return "local review or release evidence"

    return None


def test_tracked_files_exclude_generated_runtime_operator_and_agent_state():
    violations = [f"{path}: {_prohibited_reason(path)}" for path in _tracked_files() if _prohibited_reason(path)]

    assert not violations, "Prohibited generated/runtime files are tracked:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    "forced_path",
    (
        ".env.production",
        "credentials.yaml",
        "config.local.yaml",
        "foo.pyc",
        "forced.sqlite-wal",
        "forced.sqlite3-shm",
        "forced.db-journal",
        ".agents/foo.md",
        ".antigravitycli/foo.json",
        ".claude/session.json",
        "review/report.md",
    ),
)
def test_prohibition_predicate_rejects_forced_tracked_local_state(forced_path):
    assert _prohibited_reason(forced_path), f"forced-tracked local state escaped classification: {forced_path}"


def test_prohibition_predicate_keeps_non_database_journal_source_visible():
    assert _prohibited_reason("scripts/systemd-journal") is None


def test_agent_skill_paths_are_local_state():
    for path in (
        ".agents/skills/release-check",
        ".claude/skills/release-check/SKILL.md",
        ".claude/skills/astinuslab-quality-review/agents/openai.yaml",
    ):
        assert _prohibited_reason(path) == "local agent state"


def test_tracked_ignored_files_match_the_exact_transition_allowlist():
    assert _tracked_ignored_files() == _TRANSITIONAL_TRACKED_IGNORED


def test_root_plans_contains_no_tracked_files():
    tracked_root_plans = {path for path in _tracked_files() if path.startswith("plans/")}

    assert tracked_root_plans == _ROOT_PLAN_CONTRACTS
    assert len(tracked_root_plans) == 0


def test_docs_superpowers_is_entirely_tool_local_and_untracked():
    tracked = [path for path in _tracked_files() if path.startswith("docs/superpowers/")]

    assert tracked == []
    assert _check_ignored("docs/superpowers/local-prompt.md")


def test_internal_records_are_untracked_and_excluded_from_mkdocs():
    """Plans, remediation records and audits are maintainer history, not source."""
    tracked_internal = [path for path in _tracked_files() if path.startswith(_INTERNAL_RECORD_ROOTS)]
    mkdocs = MKDOCS_CONFIG.read_text(encoding="utf-8")
    config = yaml.safe_load(mkdocs)
    i18n = next(plugin["i18n"] for plugin in config["plugins"] if isinstance(plugin, dict) and "i18n" in plugin)

    assert tracked_internal == []
    assert "exclude_docs: |\n  plans/**" in mkdocs
    assert i18n["fallback_to_default"] is True


def test_built_documentation_has_no_markdown_link_into_excluded_plans():
    violations = []
    durable_root = (ROOT / "docs/plans").resolve()
    for source in _published_doc_sources():
        text = _INLINE_CODE.sub("", _FENCED_CODE.sub("", source.read_text(encoding="utf-8")))
        targets = [*_MARKDOWN_LINK.findall(text), *_MARKDOWN_REFERENCE_TARGET.findall(text)]
        for raw_target in targets:
            parsed = urlsplit(unquote(raw_target.strip("<>")))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (
                ROOT / parsed.path.lstrip("/") if parsed.path.startswith("/docs/") else source.parent / parsed.path
            ).resolve()
            if target == durable_root or durable_root in target.parents:
                violations.append(f"{source.relative_to(ROOT)} -> {raw_target}")

    assert violations == [], "published documentation links into excluded docs/plans:\n" + "\n".join(violations)


def _has_stale_plan_reference(line: str, old_path: str) -> bool:
    # The moved root-level plans are suffixes of their valid docs/plans homes.
    prefix = r"(?<!docs/)" if old_path.startswith("plans/") else ""
    return re.search(prefix + re.escape(old_path), line) is not None


def test_stale_plan_reference_detection_preserves_current_docs_paths():
    old_path = _MOVED_OLD_PATHS[0]
    assert not _has_stale_plan_reference(f"[docs/{old_path}](../../../docs/{old_path})", old_path)
    assert _has_stale_plan_reference(f"[{old_path}](../../../{old_path})", old_path)
    assert _has_stale_plan_reference(f"docs/{old_path} and {old_path}", old_path)
    assert _has_stale_plan_reference(f"`{_STALE_HERMES_PLAN_PREFIX}execution.md`", _STALE_HERMES_PLAN_PREFIX)


def test_tracked_current_tree_text_has_no_stale_moved_or_hermes_plan_reference():
    violations = []
    forbidden = (*_MOVED_OLD_PATHS, _STALE_HERMES_PLAN_PREFIX)
    # Collection transcripts contain this guard's deliberately forbidden
    # fixture path. It is a test ID, not a current-tree documentation link.
    fixture_node_id = (
        "tests/python/tooling/test_repository_hygiene.py::"
        "test_gitignore_covers_each_prohibited_generated_or_runtime_class["
        + _STALE_HERMES_PLAN_PREFIX
        + "execution.md]"
    )
    for relative in _tracked_files():
        if relative == "tests/python/tooling/test_repository_hygiene.py":
            continue
        text = _tracked_text(relative)
        if text is None:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line == fixture_node_id:
                continue
            for old_path in forbidden:
                if _has_stale_plan_reference(line, old_path):
                    violations.append(f"{relative}:{line_number}: {old_path}")

    assert violations == [], "tracked text cites stale current-tree plan paths:\n" + "\n".join(violations)


def test_repository_ignore_check_rejects_a_user_global_only_match(tmp_path):
    global_excludes = tmp_path / "global-excludes"
    global_excludes.write_text("*.md\n", encoding="utf-8")

    assert not _check_ignored(
        "not-covered-by-repository.md",
        git_config=("-c", f"core.excludesFile={global_excludes}"),
    )
    assert (
        _tracked_ignored_files(
            git_config=("-c", f"core.excludesFile={global_excludes}"),
        )
        == _TRANSITIONAL_TRACKED_IGNORED
    )


@pytest.mark.parametrize(
    "sentinel",
    (
        ".coverage.worker-1",
        ".hypothesis/examples/state",
        ".nox/lint/bin/python",
        ".tox/py312/bin/python",
        ".venv/bin/python",
        "src/astinuslab/__pycache__/main.pyc",
        "coverage.json",
        "coverage.xml",
        "example.sqlite",
        "example.sqlite3",
        "example.sqlite-wal",
        "example.sqlite-shm",
        "example.sqlite-journal",
        "example.sqlite3-wal",
        "example.sqlite3-shm",
        "example.sqlite3-journal",
        "example.db-journal",
        "graphify-out/index.json",
        "htmlcov/index.html",
        "node_modules/pkg/index.js",
        "package.egg-info/PKG-INFO",
        "playwright-report/index.html",
        "site/index.html",
        "storage/operator-owned-file",
        "test-results/results.json",
        "venv/bin/python",
        "vol3/symbols/windows/generated-isf.json",
        "images/definitions/operator-image.qcow2",
        "images/definitions/installer.iso",
        "storage/dumps/capture.mem",
        "config/catalog/targets.yaml",
        "config/catalog/targets.yml",
        "config/astinuslab.env",
        "backend/.env.production",
        ".claude/settings.local.json",
        ".hermes" + "/plans/execution.md",
        ".superpowers/sdd/task-99-report.md",
        "plans/2026-08-26-release-evidence/private.json",
    ),
)
def test_gitignore_covers_each_prohibited_generated_or_runtime_class(sentinel):
    assert _check_ignored(sentinel), f".gitignore does not cover prohibited local path: {sentinel}"


@pytest.mark.parametrize(
    "source_path",
    (
        "astinuslab.toml.example",
        "backend/.env.example",
        "backend/service.example.env",
        "config/catalog/targets.example.yaml",
        "config/catalog/targets.example.yml",
        "docs/database-journal.md",
        "docs/plans/2026-08-26-durable-plan.md",
        "images/definitions/operator-image.qcow2.meta.json",
        "scripts/systemd-journal",
        "storage/.gitkeep",
    ),
)
def test_gitignore_keeps_explicit_source_examples_and_placeholders_visible(source_path):
    assert not _check_ignored(source_path), f".gitignore hides reviewable source or placeholder: {source_path}"


def test_storage_placeholder_is_the_only_tracked_runtime_root_entry():
    assert [path for path in _tracked_files() if path.startswith("storage/")] == ["storage/.gitkeep"]


def test_tracked_vm_fixtures_are_zero_byte_discovery_sentinels():
    fixtures = [path for path in _tracked_files() if path.startswith("tests/browser/fixtures/images/")]
    binary_fixtures = [path for path in fixtures if PurePosixPath(path).suffix.lower() in _VM_IMAGE_SUFFIXES]

    assert binary_fixtures, "the browser discovery contract must retain its named image fixtures"
    assert all((ROOT / path).stat().st_size == 0 for path in binary_fixtures)


def test_archive_policy_has_no_stale_exceptions_for_untracked_agent_state():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert "/.hermes export-ignore" not in attributes
    assert "/.superpowers export-ignore" not in attributes


def test_sensitive_path_policy_names_the_implemented_secret_scanner():
    """The policy must name the scanner that exists, not one still to be chosen.

    The original text recorded the gate as unavailable because nothing was
    installed. That wording is now the dangerous one: a reader who follows it
    would record "unavailable, not passed" for a gate that has a pinned scanner,
    a command, and a CI job.
    """
    policy = GENERATED_RUNTIME_DOC.read_text(encoding="utf-8")
    required_fragments = (
        "approved secret scanner",
        "hidden tool directories",
        "deployment configuration",
        "CI configuration",
        "safe examples",
        "Gitleaks 8.30.1",
        "tools/quality/secret_scan.py --scope all",
        "fails closed",
    )

    missing = [fragment for fragment in required_fragments if fragment not in policy]
    assert not missing, f"secret-scan policy is incomplete: {missing}"

    assert "no approved scanner is currently configured or discoverable" not in policy, (
        "the unavailable-scanner policy is stale: the scanner is implemented"
    )
