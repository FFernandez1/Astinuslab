"""Run the repository's pinned secret scanner over tracked files and Git history.

One command backs both the local pre-merge gate documented in
`docs/development/GENERATED_RUNTIME_FILES.md` and the CI `secret-scan` job, so a
developer and CI cannot disagree about what "the scan passed" means:

    python tools/quality/secret_scan.py --scope all

Scopes:

* ``tracked`` — both index blobs and working-tree content of every file Git
  tracks, copied into separate throwaway snapshots. Scanning the checkout in place would also read
  ignored runtime state (`astinuslab.toml`, `.env`, the live `config/catalog/*.yaml`
  twins, `storage/`), which is exactly where real operator credentials live. A
  finding there is a local-machine fact, not a repository defect, and printing it
  would leak an operator secret into CI logs. The snapshot is built from
  ``git ls-files``, so only files that are actually in the repository are read.
* ``history`` — every commit reachable from every local ref. Deleting a leaked
  credential from the tip does not unleak it, so the tracked scan alone is not a
  gate. In a linked worktree the scan is pointed at the main checkout that owns
  the shared object store, so history is the repository's, not this worktree's
  single branch.
* ``all`` — both, and the exit status is the worst of the two.

Failure is closed. A missing scanner, a version other than the pinned one, an
unreadable or weakened configuration, a scanner crash, or any finding all exit
non-zero. There is no reporting-only mode and no baseline: a gate that can pass
because the tool was absent is not a gate.

Findings are printed as rule id, path, line, and commit only. Secret values,
matched text, and the surrounding source line are never printed, and the scanner
is run with ``--redact`` so they are not written to the intermediate report
either. Investigate a finding by opening the named location yourself.

Exit codes: 0 clean, 1 findings, 2 the scan could not be trusted to run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / ".gitleaks.toml"

#: The scanner is pinned exactly. Detection rules change between Gitleaks
#: releases, so "8.30.x" would let a rule set drift in or out from under the
#: gate without anyone deciding to accept it. Bumping this is a reviewed change:
#: update the version, the CI checksum, and re-run both scopes.
SCANNER_NAME = "gitleaks"
REQUIRED_VERSION = "8.30.1"
# Pins reviewed checksum and exact prose exceptions, not arbitrary exclusions.
REVIEWED_ALLOWLISTS_SHA256 = "b805ae3d04ad3f2b953460db1b16beb65a432f94480aa44edb2c3eaae1b6d5c4"

#: Report fields that are safe to print. Everything else in a Gitleaks finding
#: -- `Secret`, `Match`, `Line` -- carries the credential or its context.
SAFE_FIELDS = ("RuleID", "File", "StartLine", "Commit")

# These directory aliases are part of the portable instruction layout. They
# are scanned as link-text blobs, never followed into the working filesystem.
PORTABLE_SKILL_LINKS = {
    f".agents/skills/{name}": f"../../.claude/skills/{name}"
    for name in ("astinuslab-development", "astinuslab-quality-review", "release-check")
}

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_UNTRUSTED = 2


class ScanError(RuntimeError):
    """The scan could not be trusted to run, so the gate fails closed."""


def _run(command: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    # Git context variables must not redirect this gate to another index/repo.
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "GITLEAKS_"))}
    env.update(
        GIT_NO_REPLACE_OBJECTS="1", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0"
    )
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(command),
        env=env,
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def resolve_scanner(explicit: str | None) -> Path:
    """Locate the scanner and prove it is the pinned version, or fail closed."""
    candidate = explicit or os.environ.get("ASTINUSLAB_GITLEAKS_BIN") or shutil.which(SCANNER_NAME)
    if not candidate:
        raise ScanError(
            f"{SCANNER_NAME} {REQUIRED_VERSION} is not installed. Install the pinned release "
            "(verify its checksum against the upstream gitleaks_<version>_checksums.txt), or "
            "point --gitleaks / ASTINUSLAB_GITLEAKS_BIN at it. The gate does not pass without it."
        )

    binary = Path(candidate).expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ScanError(f"{binary} is not an executable file")

    result = _run([str(binary), "version"])
    if result.returncode != 0:
        raise ScanError(f"`{binary} version` exited {result.returncode}")

    reported = result.stdout.strip()
    if reported != REQUIRED_VERSION:
        raise ScanError(
            f"{binary} reports version {reported!r}, but this repository pins "
            f"{REQUIRED_VERSION!r}. Detection rules differ between releases, so a different "
            "version is a different gate. Install the pinned release."
        )
    return binary


def verify_configuration() -> None:
    """Refuse to run against a configuration that has been blinded."""
    if CONFIG_PATH.is_symlink() or not CONFIG_PATH.is_file():
        raise ScanError(f"missing {CONFIG_PATH.relative_to(REPO_ROOT)}; the scan has no configuration to trust")

    try:
        config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ScanError("scanner configuration is unreadable or invalid TOML") from error
    # Default rules plus the pinned exact exceptions are reviewed.
    # Structural parsing prevents disabledRules from weakening the gate.
    extension = config.get("extend", {})
    if (
        set(config) - {"title", "extend", "allowlists"}
        or extension != {"useDefault": True}
        or extension.get("useDefault") is not True
    ):
        raise ScanError("scanner configuration must enable unmodified default rules")
    exceptions = config.get("allowlists", [])
    if (
        exceptions
        and hashlib.sha256(json.dumps(exceptions, sort_keys=True).encode()).hexdigest() != REVIEWED_ALLOWLISTS_SHA256
    ):
        raise ScanError("scanner allowlists differ from the exact reviewed checksum/prose exceptions")

    ignore_file = REPO_ROOT / ".gitleaksignore"
    if ignore_file.exists() or ignore_file.is_symlink():
        raise ScanError(
            ".gitleaksignore exists. Fingerprint suppressions bypass this gate invisibly; remove "
            "the file or document the exception as a configuration allowlist instead."
        )


def _tracked_entries() -> list[tuple[str, str, str]]:
    result = _run(["git", "ls-files", "-z", "--stage"], cwd=REPO_ROOT)
    if result.returncode != 0:
        raise ScanError("git ls-files failed")
    entries = []
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        metadata, relative = entry.split("\t", 1)
        mode, oid, stage = metadata.split()
        if stage != "0" or mode not in {"100644", "100755", "120000"}:
            raise ScanError(f"unmerged, symlink, or submodule entry requires separate review: {relative}")
        entries.append((mode, oid, relative))
    modes = {relative: mode for mode, _, relative in entries}
    for mode, oid, relative in entries:
        if mode != "120000":
            continue
        expected = PORTABLE_SKILL_LINKS.get(relative)
        if expected is None:
            raise ScanError(f"unapproved tracked symlink requires separate review: {relative}")
        blob = _run(["git", "cat-file", "blob", oid], cwd=REPO_ROOT)
        if blob.returncode != 0 or blob.stdout != expected:
            raise ScanError(f"portable skill link differs from its approved target: {relative}")
        canonical = f".claude/skills/{Path(relative).name}/SKILL.md"
        if modes.get(canonical) not in {"100644", "100755"}:
            raise ScanError(f"portable skill lacks its tracked canonical source: {relative}")
    return entries


def _build_index_snapshot(destination: Path) -> int:
    entries = _tracked_entries()
    for _, oid, relative in entries:
        target = destination / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts or target.exists():
            raise ScanError("unsafe or colliding tracked path")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Capture bytes directly; text decoding must not alter binary blobs.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
        with target.open("xb") as output:
            result = subprocess.run(
                ["git", "cat-file", "blob", oid],
                cwd=REPO_ROOT,
                env=env,
                stdout=output,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=180,
            )
        if result.returncode != 0:
            raise ScanError("cannot read a tracked index blob")
    if not entries:
        raise ScanError("the index snapshot is empty; refusing a vacuous pass")
    return len(entries)


def _build_tracked_snapshot(destination: Path) -> int:
    """Copy tracked working-tree files into `destination`, skipping ignored state."""
    copied = 0
    for mode, _, relative in _tracked_entries():
        source = REPO_ROOT / relative
        # A symlink in any component could redirect reads into ignored operator
        # state. Fail rather than silently omit tracked content from the scan.
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ScanError("Git returned an unsafe tracked path")
        if any((REPO_ROOT / part).is_symlink() for part in relative_path.parents):
            raise ScanError(f"tracked path contains a symlink: {relative}")
        if mode == "120000":
            expected = PORTABLE_SKILL_LINKS[relative]
            if not source.is_symlink() or os.readlink(source) != expected:
                raise ScanError(f"working skill link differs from its approved target: {relative}")
            canonical = Path(".claude/skills") / relative_path.name / "SKILL.md"
            if any((REPO_ROOT / part).is_symlink() for part in (canonical, *canonical.parents)):
                raise ScanError(f"canonical skill path contains a symlink: {relative}")
            if not (REPO_ROOT / canonical).is_file():
                raise ScanError(f"canonical skill source is missing: {relative}")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(os.fsencode(expected))
            copied += 1
            continue
        if source.is_symlink():
            raise ScanError(f"tracked path contains a symlink: {relative}")
        if not source.exists():  # tracked deletion; its old content is scanned in history
            continue
        if not source.is_file():
            raise ScanError(f"tracked path is not a regular file (submodules need a separate scan): {relative}")
        target = destination / relative
        if target.exists():
            raise ScanError("tracked snapshot path collision")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied += 1
    if copied == 0:
        raise ScanError("the tracked-file snapshot is empty; refusing to report a vacuous pass")
    return copied


def history_repository() -> Path:
    """Return the checkout whose history to scan.

    In a linked worktree `.git` is a file and the objects live in the main
    checkout. Scanning the main checkout keeps `--all` meaning every local ref.
    """
    shallow = _run(["git", "rev-parse", "--is-shallow-repository"], cwd=REPO_ROOT)
    if shallow.returncode != 0 or shallow.stdout.strip() != "false":
        raise ScanError("full history is unavailable; fetch the complete history (git fetch --unshallow)")
    grafts = _run(["git", "rev-parse", "--git-path", "info/grafts"], cwd=REPO_ROOT)
    if grafts.returncode != 0 or (REPO_ROOT / grafts.stdout.strip()).exists():
        raise ScanError("legacy Git grafts can hide history; remove them before scanning")
    commits = _run(["git", "rev-list", "--all", "--count"], cwd=REPO_ROOT)
    if commits.returncode != 0 or not commits.stdout.strip().isdigit() or int(commits.stdout.strip()) == 0:
        raise ScanError("no readable committed history; refusing an empty history scan")
    result = _run(["git", "rev-parse", "--git-common-dir"], cwd=REPO_ROOT)
    if result.returncode != 0:
        raise ScanError(f"`git rev-parse --git-common-dir` failed: {result.stderr.strip()}")

    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = (REPO_ROOT / common).resolve()

    owner = common.parent if common.name == ".git" else common
    probe = _run(["git", "rev-parse", "--show-toplevel"], cwd=owner)
    if probe.returncode != 0:
        # A bare or otherwise unusual layout: fall back to this checkout, whose
        # refs still resolve against the same shared object store.
        return REPO_ROOT
    return Path(probe.stdout.strip())


def _redacted(findings: Iterable[dict]) -> list[str]:
    lines = []
    for finding in findings:
        parts = []
        for field in SAFE_FIELDS:
            value = finding.get(field)
            if value not in (None, "", 0):
                parts.append(f"{field}={value}")
        lines.append("  " + " ".join(parts) if parts else "  <finding with no locatable fields>")
    return lines


def _scan(binary: Path, scanner_args: Sequence[str], *, label: str, cwd: Path) -> list[dict]:
    """Run one scanner invocation and return its findings, redacted by the scanner."""
    with tempfile.TemporaryDirectory(prefix="astinuslab-secret-scan-report-") as report_dir:
        report = Path(report_dir) / "findings.json"
        command = [
            str(binary),
            *scanner_args,
            "--config",
            str(CONFIG_PATH),
            # `-i` is otherwise resolved against the process working directory,
            # where a stray .gitleaksignore would silently suppress findings.
            "--gitleaks-ignore-path",
            str(REPO_ROOT),
            # `=` is required: --redact has an optional value, so `--redact 100`
            # would leave 100 as a stray positional argument.
            "--redact=100",
            "--ignore-gitleaks-allow",
            "--report-format",
            "json",
            "--report-path",
            str(report),
            "--no-banner",
            "--no-color",
            "--exit-code",
            "1",
        ]
        result = _run(command, cwd=cwd)
        if result.returncode not in (0, 1):
            # Any other status is a scanner error, not a clean scan.
            raise ScanError(f"{label} scan: {SCANNER_NAME} exited {result.returncode}; scanner diagnostics withheld")

        if not report.is_file():
            raise ScanError(f"{label} scan: {SCANNER_NAME} wrote no report; treating as untrusted")
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ScanError(f"{label} scan: unreadable report ({error})") from error

    if not isinstance(payload, list) or any(not isinstance(finding, dict) for finding in payload):
        raise ScanError(f"{label} scan: invalid report structure")
    findings = payload
    if result.returncode == 1 and not findings:
        raise ScanError(f"{label} scan: {SCANNER_NAME} reported failure with an empty report")
    return findings


def scan_tracked(binary: Path) -> list[dict]:
    findings = []
    # The index may contain a staged secret that was subsequently removed from
    # disk. The working copy may contain an unstaged secret absent from the index.
    for label, builder in (("index", _build_index_snapshot), ("working tree", _build_tracked_snapshot)):
        with tempfile.TemporaryDirectory(prefix="astinuslab-secret-scan-tracked-") as snapshot_dir:
            snapshot = Path(snapshot_dir)
            count = builder(snapshot)
            print(f"tracked {label}: scanning {count} files (untracked runtime state excluded)")
            # Relative input keeps paths identical to Git history for exact allowlists.
            found = _scan(binary, ["dir", "."], label=label, cwd=snapshot)
            for finding in found:
                path = finding.get("File")
                if isinstance(path, str) and os.path.isabs(path):
                    finding["File"] = os.path.relpath(path, str(snapshot))
            findings.extend(found)
    return findings


def scan_history(binary: Path) -> list[dict]:
    repository = history_repository()
    print(f"history: scanning every commit reachable from every local ref in {repository}")
    return _scan(
        binary,
        [
            "git",
            "--log-opts=--full-history --all --diff-merges=separate --no-ext-diff --no-textconv --text",
            str(repository),
        ],
        label="history",
        cwd=repository,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="secret_scan.py",
        description=f"Scan this repository with the pinned {SCANNER_NAME} {REQUIRED_VERSION}.",
    )
    parser.add_argument(
        "--scope",
        choices=("tracked", "history", "all"),
        default="all",
        help="tracked files, all local history, or both (default: all)",
    )
    parser.add_argument(
        "--gitleaks",
        default=None,
        help=f"path to the pinned {SCANNER_NAME} binary (default: ASTINUSLAB_GITLEAKS_BIN, then PATH)",
    )
    args = parser.parse_args(argv)

    try:
        verify_configuration()
        binary = resolve_scanner(args.gitleaks)
        print(f"{SCANNER_NAME} {REQUIRED_VERSION} at {binary}")

        findings: list[tuple[str, list[dict]]] = []
        if args.scope in ("tracked", "all"):
            findings.append(("tracked", scan_tracked(binary)))
        if args.scope in ("history", "all"):
            findings.append(("history", scan_history(binary)))
    except ScanError as error:
        print(f"secret scan FAILED (untrusted): {error}", file=sys.stderr)
        return EXIT_UNTRUSTED
    except (OSError, subprocess.SubprocessError, UnicodeError):
        print("secret scan FAILED (untrusted): tool or filesystem operation failed", file=sys.stderr)
        return EXIT_UNTRUSTED

    total = sum(len(found) for _, found in findings)
    if total:
        print(f"\nsecret scan FAILED: {total} finding(s). Values are redacted; open each location.", file=sys.stderr)
        for label, found in findings:
            if found:
                print(f"{label}:", file=sys.stderr)
                print("\n".join(_redacted(found)), file=sys.stderr)
        return EXIT_FINDINGS

    scopes = ", ".join(label for label, _ in findings)
    print(f"\nsecret scan PASS: {SCANNER_NAME} {REQUIRED_VERSION}, scope(s) {scopes}, 0 findings")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
