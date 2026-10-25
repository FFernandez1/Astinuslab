# Generated and Runtime Files

`.gitignore` is the authoritative source-control boundary for generated,
runtime, local-configuration, and operator-owned files. Tool-specific ignore
files may additionally keep bulky local data out of an assistant's context,
but they do not decide what Git may track.

Generated output is not source by default. It may be committed only when it is
a reviewed release input or durable evidence. That commit must name the owner,
the producer and exact refresh command, and why regeneration is insufficient.
Private evidence, credentials, live configuration, VM bytes, memory captures,
databases, logs, and generated symbols are never eligible for that exception.

## Producer and ownership matrix

| Path or artifact | Producer and refresh command | Owner | Tracking and cleanup policy |
| --- | --- | --- | --- |
| `site/` | MkDocs: `.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict` | Documentation maintainers | Built documentation. Never track. Let MkDocs replace it with its normal clean build after documentation builds and servers stop. |
| `graphify-out/` | Graphify: `graphify update .` | Contributor running structural analysis | Derived AST/index data. Never track. Re-run Graphify after source changes; remove only when no analysis process is using it. |
| `test-results/`, `playwright-report/` | Playwright commands in `package.json`, for example `npm run test:smoke` | Browser-test maintainers | Per-run reports, traces, screenshots, and attachments. Never track. Retain failed-run output until triage finishes; then remove the selected run. |
| `.coverage`, `.coverage.*`, `coverage.xml`, `coverage.json`, `htmlcov/` | Coverage.py / pytest-cov; HTML refresh: `.venv/bin/python -m coverage html` | Python-test maintainers | Per-run measurements and reports. Never track. Merge distributed data before cleanup when a combined result is required. |
| `build/`, `dist/`, `*.egg-info/` | Python build and editable/install metadata; operator image preparation may also use `build/windows/` | Packaging maintainers or the operator running an image build | Generated package metadata and scratch artifacts. Never track ordinary local output. Setuptools can retain removed package files in an existing `build/lib/`, so release manifests must be produced from a clean build directory or clean exported source snapshot and inspected before custody. `build/windows/` may contain seed media, rendered Autounattend files, screenshots, temporary scripts, or VM disks; reusable helpers belong in `ops/host/windows_build/`, reusable guest templates in `guest/windows/`, and new per-run scratch should prefer `storage/tmp/windows-build/<id>/`. A frozen release artifact belongs in external release custody with its source SHA and checksums, not in the checkout. |
| generated environment reference in `docs/usage/configuration.md` | `.venv/bin/python tools/dev/generate_env_reference.py` | Backend configuration maintainers | Reviewed, tracked release input. Refresh in the same change as `Settings`; `tests/python/test_env_reference.py` verifies parity. Do not hand-edit the generated block. |
| private release evidence and reviewed summaries | `tools/release/collect_release_evidence.sh`, then `tools/release/validate_release_evidence.py --candidate-sha "$(git rev-parse HEAD)" <bundle>` | Named release owner | Generate raw bundles outside the checkout under the configured evidence root; private bundles remain untracked. Publish only a reviewed summary with explicit candidate scope and limitations; never copy raw bundles into the repository. |
| root `plans/` evidence workspace | Release review and acceptance procedures | Named release owner | Raw evidence is external, ignored, and never tracked. Planning and review records are kept outside the public repository. |
| `storage/` and `*.db`, `*.sqlite*`, `*.log`, `*.dump` | API, workers, Celery, tests, and VM jobs | Operator or developer who owns the running instance | Mutable runtime state. Never track except `storage/.gitkeep`. Use application/API cleanup after all services and jobs stop; back up required evidence first. |
| `images/definitions/*.{img,qcow2,raw,vdi,vhd,vhdx,vmdk,iso}` | Image download and operator build scripts | Image/catalog owner | Operator-owned VM or installation bytes. Never track ordinary images. Tracked metadata is reviewable provenance, not permission to delete the corresponding local image. The zero-byte browser fixtures are the documented discovery-only exception. |
| `vol3/symbols/` | Volatility symbol download/build workflows | Forensics maintainer and operator | Generated, kernel-specific runtime cache. Never track generated symbols. Rebuild from matching debug artifacts; remove only the selected symbol after analysis and build jobs stop. |
| `.venv/`, `venv/`, `node_modules/`, Python/pytest/Ruff/Mypy caches | Python and Node dependency/test tools | Local contributor | Reproducible local dependency and cache state. Never track. Recreate from the lockfiles; remove only when no interpreter, test, linter, docs server, or editor integration uses it. |
| `.claude/`, `.agents/`, `.hermes/`, `.superpowers/`, `docs/superpowers/`, and other ignored agent workspaces | Local agent sessions | Local contributor | Local execution state, never durable repository history. Stages A and B untrack reviewed tool-local files with explicit `git rm --cached` paths without deleting local copies. Reviewed product/contributor conclusions belong in normal built `docs/`. |

## Untracking is not deletion

Stopping version control for a local artifact and deleting its bytes are
different operations. Review the exact tracked list, then use explicit paths:

```bash
git ls-files -- .hermes .superpowers docs/superpowers
git rm --cached -- <reviewed-exact-path> [<reviewed-exact-path> ...]
git diff --cached --name-status -- .hermes .superpowers docs/superpowers
```

Do not use recursive index removal or filesystem deletion for `storage/`,
`site/`, `images/definitions/`, `vol3/symbols/`, virtual environments, or agent
directories. `git rm --cached` leaves local bytes in place; a commit revert
restores tracking without pretending to restore or roll back runtime data.

## Safe cleanup procedure

Cleanup is a separate, explicit operator action. Before any runtime cleanup:

1. Stop AstinusLab services with `./astinuslab.sh stop`.
2. Confirm there are no active API processes, Celery tasks, VM/image/symbol
   jobs, tests, documentation servers, editors using an environment, or agent
   sessions using the target path.
3. Back up logs, dumps, databases, evidence, images, symbols, or keys that must
   be retained.
4. Prefer the owning tool's cleanup or refresh command. Select the exact path;
   never use a repository-wide recursive deletion.

Examples for reproducible, non-runtime reports after their producers stop:

```bash
rm -f .coverage .coverage.* coverage.xml coverage.json
rm -rf htmlcov/ test-results/ playwright-report/
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict
```

For application state, use the UI, API, or `./astinuslab.sh cleanup`; do not
remove `storage/` wholesale. For images and symbols, delete only the reviewed
image or symbol target after proving no catalog, overlay, VM, analysis, or build
job depends on it. Environment removal is also an explicit rebuild decision,
not routine repository cleanup.

## Configuration and credential boundary

Only clearly classified safe examples belong in source control, such as
`astinuslab.toml.example` and `config/catalog/*.example.yaml`. Live catalog
twins, `astinuslab.toml`, environment files, private keys, credential stores,
and local settings remain ignored. Example values must be non-secret fixtures;
the word `example` does not make a real credential safe.

Changes under hidden tool directories, deployment configuration, CI configuration,
or safe examples require a run with the repository's approved secret scanner
before staging contents or merging. Record the scanner, version, scope, and PASS result
with the change. A manual grep is not an approved secret scanner.

The approved scanner is **Gitleaks 8.30.1**, pinned to that exact version and
configured by `.gitleaks.toml` with upstream default rules and the two exact
historical checksum exceptions documented below.
Run it through the one supported command:

```bash
python3 tools/quality/secret_scan.py --scope all
```

For new source files, first run `git add --intent-to-add -- path/to/new/file`
so `git ls-files` includes them without staging their contents. Untracked files
are outside the tracked scope; this command cannot certify a file it never reads.

`--scope tracked` scans both index blobs and the working-tree content of every
file `git ls-files` reports, using separate throwaway snapshots. This catches a
secret staged and then removed from disk, as well as an unstaged source edit.
Untracked runtime state (`astinuslab.toml`, environment files, live catalogs,
`storage/`) is excluded. A runtime file forcibly tracked with `git add -f` is
scanned even if its name matches `.gitignore`.
`--scope history` covers every commit reachable from every local ref, including
merge-resolution changes through separate parent diffs, because
deleting a credential from the tip does not unleak it; inside a linked worktree
it scans the main checkout that owns the shared object store. `--scope all`
runs both and is what the `secret-scan` CI job invokes after a `fetch-depth: 0`
checkout, with the release archive verified against the SHA-256 published in
`gitleaks_8.30.1_checksums.txt`.

The gate fails closed. A shallow checkout, missing scanner, a version other
than 8.30.1, a weakened configuration, a scanner error, or any finding exits non-zero (2 for an
untrusted run, 1 for findings); there is no reporting-only mode and no baseline.
Git context overrides cannot redirect the gate to a different repository/index;
replace objects are disabled and legacy grafts are rejected.
Fetch a shallow clone's full history with `git fetch --unshallow` before running
history checks. Inline `gitleaks:allow` comments cannot suppress findings.
Tracked symlinks and submodules fail closed; submodule repositories require
separate scans. Findings print as rule id, path, line, and commit only — investigate by opening
the named location, and never paste a secret value into an issue, a log, or this
document.

Install the pinned scanner on the supported Linux x86_64 host with the same
archive and checksum as CI. The hash below is from the
[official Gitleaks 8.30.1 checksums](https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt):

```bash
(
  set -euo pipefail
  scan_download_dir=$(mktemp -d)
  trap 'rm -rf "$scan_download_dir"' EXIT
  cd "$scan_download_dir"
  curl --fail --silent --show-error --location --retry 3 \
    -o gitleaks.tar.gz \
    https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz
  echo '551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb  gitleaks.tar.gz' | sha256sum --check --strict
  tar -xzf gitleaks.tar.gz gitleaks
  install -Dm755 gitleaks "$HOME/.local/bin/gitleaks"
)
export PATH="$HOME/.local/bin:$PATH"
python3 tools/quality/secret_scan.py --scope all
```

Alternatively, `--gitleaks /absolute/path/to/gitleaks` or
`ASTINUSLAB_GITLEAKS_BIN` selects an already installed binary. Other platforms
must verify the matching official archive checksum; the scanner version stays
8.30.1. Scans run locally and do not send repository contents to a service.

A finding is a blocker to investigate and remediate — rotate the credential
first, then remove it from history — not an entry to add to an allowlist.
Directory-wide, glob-wide, or rule-wide allowlists (`tests/`, `docs/`,
`*.example.*`) are prohibited: they blind the scanner in exactly the places
contributors paste real credentials by accident. A proven synthetic fixture that
genuinely has to be excepted needs an exact path plus the exact rule id, with the
fixture's provenance recorded in this document.

## Clean-clone check

Run the focused policy test before merging:

```bash
.venv/bin/python -m pytest tests/python/tooling/test_repository_hygiene.py -q
```

It evaluates `git ls-files`, not the working directory, so ignored local files
may remain present. A clean clone must have no prohibited tracked generated or
runtime files. The documented zero-byte browser image fixtures and
`storage/.gitkeep` are explicit exceptions; adding another exception requires a
reviewed owner and lifecycle policy.

## Reviewed historical checksum exceptions

The merge-aware scan initially reported 22 matches: 11 distinct source-checksum
lines, each seen against both parents of merge
`977599fde68ff0c78b2ff5009d879f5e366ed449`. The `generic-api-key` rule mistakes
source paths containing words such as `api`, `keystore`, or `tokens` followed by
SHA-256 values for credentials.

| Historical report | Source of the non-secret values |
| --- | --- |
| `docs/remediation/evidence/merge-to-main/source-sha256.json` | Ten reported values were independently recomputed from the named source files in the same commit and matched exactly. |
| `docs/remediation/evidence/merge-to-main/playground-merge-report.json` | One entry under `source_sha256` identifies the SHA-256 of `tests/python/playground/test_api.py` in an uncommitted merge-test snapshot. The report explicitly records that no commits were created; that snapshot hash could not be reproduced from reachable Git source versions. Classification relies on the report's checksum field and source-file context. |

Each of the two configuration entries requires **all** of: the exact report
path, `generic-api-key` rule, and exact source-path/checksum line. These same
reviewed checksum lines are permitted wherever the reports appear in Git history
(the current tree no longer tracks them); the historical commit above records their provenance. No commit, directory, filename, or detection rule is excluded wholesale.
The runner pins the canonical exception data's SHA-256, so broadening it requires
an explicit code/config review. New credentials at the same paths remain detected,
which is exercised with the actual scanner in CI. There are no credential-value
or broad test-fixture exceptions.

## Documentation publication boundary

An additional reviewed exception covers the session-expiry and revocation wording
in CL26 of `docs/plans/2026-10-03-documentation-frontend-review/claims-ledger.md`,
a historical record that the current tree no longer tracks.
This phrase describes session behavior and contains no credential. The exception
requires the exact path, `generic-api-key` rule and exact matched phrase. It is
included in the runner's pinned exception-data digest. A regression check with
the real scanner covers the full report and proves that a different credential
on that line or another line at the same path remains detected. The same prose
at another path is also scanned. No report or directory is exempted.

MkDocs publishes maintained guides and explicitly linked maintainer references.
`docs/plans/`, `docs/superpowers/`, `docs/audits/` and `docs/remediation/` are
not part of the public repository and stay excluded from the generated site and
search: neither summaries nor raw transcripts, JSON, screenshots or checksums
belong there. Promote useful conclusions into a reviewed current guide before
publication. Raw new evidence belongs outside tracked source, as described above.

`user/WINDOWS_IMAGES.md` is the current operator entry point for Windows images. Installed guide sources and wheel resources
include the maintained `user`, `usage`, `reference`, `api`, `architecture`,
`development` and `community` families plus root index, license and notices pages;
they exclude the historical directories listed above and generated site output.

English is authoritative. Until reviewed translations are available, Windows
Images, Backend Boundaries, Generated Runtime Files and Forensic Integrity use
MkDocs' English fallback in Spanish, French and Catalan. Do not restore stale
translated migration, deletion or forensic instructions to fill a locale slot.
