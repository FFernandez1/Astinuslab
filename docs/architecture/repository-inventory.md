# Repository structure inventory

> **STATUS: HISTORICAL (2026-08-29):** The staged repository migration is complete; this inventory records the evidence and decisions, not a live target.

This document records the evidence used to review and reorganize the AstinusLab repository. Raw command output and generated artifacts remain under ignored `build/structure-review/`; this file keeps the durable findings needed for review and comparison.

## Baseline

Baseline captured on 2026-08-26 before repository paths were moved.

| Field | Value |
| --- | --- |
| Commit | `10dc9b9befa43e3db91af34928939a150f2e0db6` |
| Branch | `codex/repository-structure-review` |
| Worktree | `.worktrees/repository-structure-review` |
| Python | 3.12.3 |
| Node.js | 24.14.1 |
| npm | 11.12.1 |
| Git | 2.43.0 |
| Graphify | 0.8.37 |
| ShellCheck | 0.9.0 |
| libvirt client | 10.0.0 |

The isolated worktree was clean before evidence generation. It has its own ignored Python virtual environment and `node_modules`; dependencies were installed from `requirements-dev.lock` and `package-lock.json`. The host has systemd as PID 1, `/dev/kvm`, `virsh`, ShellCheck, and Graphify.

## Baseline verification

| Gate | Result |
| --- | --- |
| Python collection | 2,900 backend tests collected |
| Backend tests | 2,883 passed, 17 skipped |
| Repository tooling tests | 760 passed, 1 skipped |
| Backend-only coverage | 86.1% |
| Combined backend/tooling coverage | 86.1% |
| Focused formatting-change tests | 276 passed, 1 skipped |
| Ruff lint | pass |
| Ruff format | pass after normalizing five pre-existing deviations |
| Mypy | pass across 356 source files |
| JavaScript lint | pass |
| Prettier check | pass |
| JavaScript unit tests | 26 passed |
| Strict multilingual MkDocs build | pass |
| Documentation browser tests | 41 passed |
| Playwright UI smoke tests | 28 passed |
| Wheel build | pass |

The five pre-existing Ruff formatting deviations were in:

- `backend/app/forensics/tool_metadata.py`
- `backend/app/forensics/vol3_guide.py`
- `backend/tests/forensics/test_vol3_guide.py`
- `tests/unit/test_docs_theme.py`
- `tests/unit/test_install_script.py`

Their changes are mechanical line wrapping and quote normalization. Ruff lint, full format checking, and focused tests pass after normalization.

## Environmental gates

Playwright browsers are installed and both documentation and application smoke suites run locally. Real-host execution is not enabled because `ASTINUSLAB_REAL_HOST` and the operator-supplied Volatility/dwarf2json path and checksum variables are unset. This is recorded as unavailable, not passing. No repository structure decision may rely on an unexecuted real-host gate.

## Baseline observations

- MkDocs reports `docs/dev/host-nightly-runner.md` as outside configured navigation for each locale; the strict build still succeeds. The structure review must decide whether this is deliberate contributor-only documentation or a navigation omission.
- Seven backend test warnings are known: six Starlette per-request-cookie deprecations and one Jinja invalid-escape deprecation.
- The current wheel builds successfully, but its content and runtime-resource ownership still require the package-layout audit.
- Raw collection, coverage XML, wheel inventory, tool versions, and test output are stored under `build/structure-review/baseline/` and are intentionally ignored.

## Tracked corpus

The baseline contains 869 tracked files across 45 root entries. The largest tracked areas are:

| Root | Files | Primary content |
| --- | ---: | --- |
| `backend/` | 368 | application, migrations, and Python tests |
| `docs/` | 183 | MkDocs source, translations, architecture, and durable plans |
| `frontend/` | 88 | Jinja templates and static browser assets |
| `scripts/` | 50 | developer, release, image, and privileged-host tools |
| `tests/` | 46 | tooling/host Python tests and browser tests |
| `guest/` | 27 | cloud-init, guest scripts, workload installers, Windows input |
| `plans/` | 22 | release-gate dependency, review prompt suite, and historical plans |
| `images/` | 19 | tracked metadata/manifests plus ignored operator image data |
| `config/` | 7 | operator environment and catalog examples |
| `deploy/` | 6 | installer, uninstaller, privilege configuration, systemd units |
| `vol3/` | 6 | AstinusLab integration docs/config/scripts; generated tools/symbols ignored |

The corpus has 384 Python, 62 shell, 69 JavaScript-family, 11 CSS, and 218 Markdown files. Raw index, size, extension, type, and ignored-path inventories are under `build/structure-review/inventory/`.

## Top-level ownership and disposition

`Keep` means the path is conventional and already has a clear responsibility. `Move` means its contents have a clearer target owner. `Split` means source and generated/runtime or multiple execution environments are currently mixed. `Untrack` means the content may remain locally but is not durable repository source.

| Current path | Classification and owner | Ships/mutable | Tests or consumers | Initial disposition |
| --- | --- | --- | --- | --- |
| `.github/` | CI and contribution policy; maintainers | source; ships in source archive only | GitHub Actions, PR process | Keep; update path consumers during moves |
| `.claude/` | release-check tool dependency | source-like local tooling; archive handling is special | `RELEASE_PLAN.md`, release dependency tests | Split: retain one canonical release-check dependency or relocate it; do not keep duplicate tool copies |
| `.hermes/` | historical agent plans | tracked tool output, export-ignored | release documentation has one stale/missing plan reference | Untrack after durable decisions are preserved and references repaired |
| `.superpowers/` | agent execution reports | tracked tool output, export-ignored | release archive boundary tests | Untrack or archive durable summaries under `docs/`; never ship as product source |
| `.aiderignore`, `.claudeignore`, `.codeiumignore`, `.continueignore`, `.cursorignore` | tool-specific ignore interfaces | source; non-runtime | five byte-identical files | Keep the five consumer entry points and test byte parity; do not introduce a generator merely to copy this small policy |
| `.gitignore`, `.gitattributes` | repository/archive policy | source; non-runtime | Git, installer/archive tests | Keep; correct the unanchored `plans/` rule and preserve archive license/source guarantees |
| `.prettierignore`, `.prettierrc.json`, `eslint.config.js` | frontend quality configuration | source; non-runtime | npm/CI | Keep at root while frontend tooling is root-owned; update exact globs with asset moves |
| `README.md`, `OVERVIEW.md`, `CHANGELOG.md`, `RELEASE_PLAN.md` | contributor/product/release entry documents | source archive | docs and release processes | Keep at root; resolve stale path references |
| `LICENSE`, `LICENSES/`, `LICENSE_COMPATIBILITY_REVIEW.md`, `THIRD_PARTY_NOTICES.md` | licensing and provenance | required source/release material; immutable | archive, wheel, and release checks | Keep at conventional root; mechanically verify wheel/release inclusion |
| `pyproject.toml`, `requirements.lock`, `requirements-dev.lock`, `uv.lock` | Python build/dependency policy | source; non-runtime | pip, setuptools, CI | Keep at root; adopt explicit package inclusion/exclusion |
| `package.json`, `package-lock.json` | browser tooling dependencies/commands | source; non-runtime | npm, CI, Playwright | Keep at root unless a true JS package boundary is introduced |
| `playwright.config.js`, `playwright.docs.config.js`, `playwright.integration.config.js` | browser suite configuration | source; non-runtime | `tests/ui`, package scripts, CI | Keep at root; update test/resource paths atomically |
| `astinuslab.sh` | development launcher and DB/service entry point | shipped source; executes on developer host | unit tests, docs, users | Keep conventional root launcher or replace with a documented console command only after parity |
| `astinuslab.toml.example` | operator configuration contract | shipped example; copied/mutated outside source | config loader, installer, docs | Keep at root unless all operator examples move together |
| `backend/` | Python app, Alembic migrations, Python tests | app/migrations ship; tests should not be in wheel | packaging, installer, systemd, pytest, all runtime entry points | Split: installable app under `src/astinuslab`, package-owned migrations, tests under unified `tests/` |
| `frontend/` | runtime templates/static assets | ships in release; immutable at runtime | FastAPI/Jinja, npm, Playwright, installer | Move under package-owned web resources if wheel/release tests pass; otherwise retain as one explicit asset root |
| `config/` | safe operator examples and catalog examples | ships; copied to mutable operator config | loaders, installer, docs, tests | Keep operator examples; decide whether catalog source remains here or moves to resources based on runtime contract |
| `deploy/` | install, upgrade, uninstall, systemd, privilege roots | ships/executes on host; writes installed/runtime state | extensive tooling and host tests | Move to `ops/deploy` and `ops/systemd` only if conventional discoverability and installer history improve; preserve executable entry documentation |
| `scripts/` | mixed developer, release, image, and privileged host tools | some ship and some are contributor-only | installer, backend path manifests, CI shell gates | Split by execution environment into `tools/dev`, `tools/release`, and `ops/host` |
| `guest/` | deployable VM-side resources | ships; executed/mutated in guests | catalogs, backend guest loaders, shell tests | Keep as a first-class product resource or move intact to `resources/guest`; do not mix with host tools |
| `images/` | tracked metadata/manifests plus ignored image binaries | metadata ships; binaries are mutable operator data | catalog, image admin, installer, provenance tests | Split source metadata from runtime image storage; never track actual images |
| `vol3/` | AstinusLab integration sources plus ignored symbols/tools | scripts/docs/config ship; symbols mutable; external tools operator-owned | installer, backend, host nightly, license checks | Split source integration from generated symbols while preserving the external-tool boundary |
| `storage/` | mutable databases, artifacts, logs, keys, caches | runtime only; never source except `.gitkeep` | app, installer, systemd, host tests | Retain compatibility default initially; make release paths absolute/configured and consider `var/` only with tested migration |
| `tests/` | tooling/host Python tests and browser suites | source; excluded from wheel but included in source archive | pytest, Playwright, CI | Unify all tests here with explicit `python`, `shell`, `browser`, and fixture ownership |
| `docs/` | canonical MkDocs source, translations, architecture, durable plans | source; built output ignored | MkDocs, browser docs tests, contributors | Keep; make `docs/plans/` truly trackable and keep generated `site/` separate |
| `plans/` | release readiness contract, review suite, durable/historical plans | source/tooling; excluded from installed release | release-check skill, host script tests, `RELEASE_PLAN.md` | Split: preserve release-gate/review tooling under a named tools/release boundary; move durable plans to `docs/plans/` |
| generated local roots (`build/`, `site/`, `test-results/`, `graphify-out/`, caches, virtual environments, `node_modules/`, package metadata) | generated evidence/build/cache | never source; reproducible and mutable | development/test tools | Keep ignored; add structural guardrails against tracking |

## Packaging and source/archive boundaries

The baseline wheel contains 356 entries under `backend/`: 144 application entries, 9 Alembic entries, and 203 test entries. It contains no `frontend/` templates or static assets. Consequences:

- wheel installs include backend tests that are not runtime dependencies;
- migrations are present because `backend*` package discovery includes them;
- the UI cannot run from the wheel alone because runtime web assets are absent;
- package selection is implicit and too broad rather than an intentional app/migration/resource manifest.

The baseline source archive intentionally includes backend/tests, root tests, documentation, and `plans/` because preferred-form source and release verification depend on them. At baseline `.hermes/` and `.superpowers/` were tracked but export-ignored. Stage A makes that archive exception unnecessary by untracking the local agent output itself. Any restructuring must preserve the license/source-archive argument while making wheel contents narrower.

## Special tracked paths

- Eight zero-byte `tests/ui/fixtures/images/*.{img,qcow2}` files are intentional browser-discovery fixtures. Their filenames exercise image classification without committing VM bytes; `tests/ui/fixtures/images/README.md` records this contract.
- `storage/.gitkeep` is the only tracked runtime-state file.
- No database, log, dump, generated symbol, built site, Graphify output, test result, or non-fixture VM image is tracked at baseline.
- At baseline, `plans/` was ignored by an unanchored `.gitignore` rule even at `docs/plans/`, so newly created canonical plans were silently ignored unless force-added. Existing tracked plan files survived only because Git already knew them.
- Five tool ignore files are currently byte-identical. They are separate consumer interfaces, not five independent policies.
- At baseline, `RELEASE_PLAN.md` named an untracked Hermes remediation plan as
  its release-record source. Stage B must replace that stale citation with a
  reviewed tracked summary before agent-local directories become noncanonical.

## Stage A hygiene result

Stage A changes tracking policy without deleting local data:

- the four tracked Hermes plan files and five `.superpowers/sdd/*` reports are removed from the index with explicit `git rm --cached` paths; all nine working-tree files remain present locally;
- `.claude/skills/release-check/SKILL.md` remains tracked until canonical release-tool packaging is proven in its owning stage;
- `.gitignore` is the authoritative policy, organized by environment/cache, build output, runtime state, operator-owned artifacts, local configuration, tests, documentation output, and agent tooling;
- the root external `/plans/` workspace remains ignored while `docs/plans/` is visible to Git and intentionally excluded from the built MkDocs site; Stage B still owns durable plan and evidence placement;
- `.gitattributes` no longer carries archive exceptions for files that are not tracked, and continues to preserve the complete preferred-form source archive;
- the five tool ignore entry points remain separate and byte-identical, with parity enforced by `tests/unit/test_repository_hygiene.py` rather than a new generator;
- `storage/.gitkeep` and the eight zero-byte browser image discovery fixtures remain the only explicit generated/runtime-style tracking exceptions.

The hygiene test evaluates `git ls-files` and rejects caches, virtual environments, generated build/docs/test/coverage/package output, databases, logs, dumps, non-fixture VM media, generated symbols, memory captures, live operator configuration, credential-bearing local settings, and local agent/review evidence. Producer, owner, refresh, and stopped-service cleanup rules are recorded in [Generated and Runtime Files](../development/GENERATED_RUNTIME_FILES.md).

The sensitive-path scan is explicitly unavailable at Stage A: no repository-approved scanner configuration or command was found, and no `gitleaks`, `detect-secrets`, `trufflehog`, `ggshield`, or `git-secrets` executable was installed. This is not a passing scan. Changes under hidden tool directories, deployment/CI configuration, or safe examples require a ratified scanner and recorded result before maintainers may claim that gate passed.

## Stage B durable-record result

Stage B establishes four ownership classes:

1. Contributor and product documentation remains normal built content under
   `docs/`, with English as its source of truth and the existing locale fallback
   policy.
2. Durable architecture and implementation history is tracked under
   `docs/plans/`. It is deliberately excluded from MkDocs publication, and built
   pages may not link into it. These internal records are English-only and do not
   require bulk locale copies.
3. Raw or private release evidence remains outside Git under the configured
   evidence root, including ignored `plans/<date>-*-evidence/` workspaces. Only a
   reviewed summary is eligible for `docs/plans/`.
4. Tool-local prompts, scratch data, and generated reports are ignored and never
   canonical.

Ten durable records move to `docs/plans/` with their basenames preserved, and
`docs/plans/2026-08-22-release-acceptance-evidence-summary.md` becomes the tracked
owner of the reviewed Task 15 and Phase 2 Gate B release record. The summary names
the external raw-evidence locations without claiming that those bundles are in
Git. The tracked `docs/superpowers/` prompt present during Stage B was explicitly
untracked with its local bytes preserved. The later portable-lab-sharing design
is retained at `docs/plans/2026-08-30-portable-local-lab-sharing-design.md`, so
`/docs/superpowers/` remains ignored as tool-local input.

Root `plans/` is an ignored external workspace and contains no tracked files.
The release gate and its 18-file prompt suite are owned by
`tools/release/review/`. The protected `.claude/skills/release-check/SKILL.md`
is now the sole tracked-but-ignored transition path. Repository topology tests
enforce those boundaries, Git visibility for `docs/plans/`, the MkDocs
exclusion, absence of tracked `docs/superpowers/`, release-record facts, and
stale-reference removal.

## Stage C branch baseline

Stage C starts from commit `5efafb471ae09172fed047b7a6bec1b1668665aa`.
Its ignored comparison evidence is stored under
`build/structure-review/baseline-5efafb471ae0/`; the recorded status is empty and
the commit is the Stage C branch point. The top-level manifest contains 43
entries. This baseline supersedes earlier counts for the test-topology move
without changing their historical value.

- Pytest collects exactly 2,900 backend tests. The backend run completes with
  2,883 passed and 17 skipped; backend-only application coverage is 86.17%
  (reported as 86.2 by `coverage report --format=total`).
- The repository tooling suite completes with 838 passed and 1 skipped. Combined
  backend-plus-tooling application coverage remains 86.17%/86.2%, so Stage C
  must preserve both the backend-only and combined floors independently.
- The wheel contains 362 total manifest entries: 356 below `backend/`, including
  144 application entries, 203 test entries, and 9 Alembic entries. Stage C must
  remove tests from that implicit package boundary without changing product
  modules; the later package stage owns the remaining application/migration
  packaging changes.
- Strict all-locale MkDocs, Ruff check and format, Mypy for 356 source files,
  JavaScript lint and formatting, 26 JavaScript unit tests, 41 documentation
  browser tests, and 28 smoke tests all pass at the branch point.
- Bare `python` is unavailable in this environment. `python3` and the isolated
  `.venv/bin/python` both report Python 3.12.3; Node is v24.14.1, npm is 11.12.1,
  and Git is 2.43.0. Stage commands use the isolated interpreter.

Before the first Python test move, collision analysis found six repeated module
basenames inside the future unified tree (`test_api.py`, `test_catalog.py`,
`test_repositories.py`, `test_startup_recovery.py`, `test_ui.py`, and
`test_vm_lifecycle.py`). There are no duplicate `test_*.py` basenames between
the current backend and tooling roots. Stage C therefore enables importlib
collection and namespace-package discovery before moving Python suites and
locks the temporary root-test allowance against additions.

## Stage C application-test result

The application-owned Python suite moves from `backend/tests/` to
`tests/python/` without product behavior changes. Domain directories, the 105
temporarily deferred root modules, suite-owned fixtures, marker assignment,
end-to-end gating, and opt-in real-host gating retain their previous contracts.
The shared helper API now has the explicit test-only import path
`tests.python._support.helpers`; all old `backend.tests` imports are prohibited.
The zero-consumer `_fakes.py` module is retained beside that API with its
noncanonical status recorded rather than silently deleted during a path move.

The moved suite still collects exactly 2,900 tests and completes with 2,883
passed and 17 skipped. Backend-only application coverage remains 86.17% (86.2%
in the integer-format comparison), equal to the Stage C branch baseline. A wheel
built from the staged tree in a clean exported snapshot contains 153 `backend/`
entries: the same 144 application and 9 Alembic entries, with zero application
or tooling test entries. An in-place setuptools build can retain files removed
from an existing ignored `build/lib/`; release/package evidence must therefore
come from a clean build directory or clean source snapshot, never from an
unexamined incremental build tree.

## Stage C tooling and host-shell result

The repository tooling suite moves from `tests/unit/` to
`tests/python/tooling/`. It remains a separate gate: recursive application
discovery skips tooling unless the tooling path is named explicitly, so bare
pytest and `pytest tests/python` retain the 2,900-test application contract,
while `pytest tests/python tests/python/tooling` collects 3,747 tests. The
explicit tooling command collects 847 tests and the executable suite remains
the owner of installer, release, repository-hygiene, and documentation checks.

The host acceptance shell matrix moves from `tests/host/` to
`tests/shell/host/`. CI keeps its recursive `tests/**/*.sh` shell policy, while
the matrix contract, release-acceptance instructions, and shell comments name
the final path. Baseline references above remain historical evidence rather
than current-path instructions.

## Stage C browser-test result

Browser-owned tests now live under `tests/browser/`: Playwright retains its
smoke, integration, full, and documentation projects, while the 26 Node unit
tests live under the mirrored `tests/browser/unit/` tree and import production
modules from `frontend/static/`. The final Playwright configuration, npm and
ESLint globs, release/archive checks, and localized development layout docs all
name the browser root. The eight tracked image discovery sentinels remain
zero-byte files in `tests/browser/fixtures/images/`; generated `*.meta.json`
sidecars remain ignored and untracked, so no fixture payload enters the product
tree.

## Stage D branch baseline

Stage D starts from `5645e811a2f5` on
`codex/repository-structure-stage-d`. The first branch-point audit found one
Ruff-format deviation introduced with the final browser contract assertion at
`c8d8d5c`; `5645e81` fixes only that formatting and is the clean baseline used
for Stage D comparisons. The rejected `c8d8d5c` evidence is retained under the
ignored structure-review output as defect evidence, not acceptance evidence.

At the corrected baseline, default application collection remains 2,900 tests;
the application suite completes with 2,883 passed and 17 skipped at 86.17%
backend coverage, and the separate tooling suite completes with 846 passed and
1 skipped. Ruff, Mypy across 357 source files, JavaScript lint and formatting,
26 JavaScript unit tests, and the strict multilingual MkDocs build pass. A
wheel built from a clean `git archive` snapshot contains 153 `backend/`
entries (144 application and 9 Alembic) and no application or tooling tests.
The in-place wheel again demonstrated that ignored stale `build/lib` content is
not valid release evidence; Stage D comparisons use only the clean snapshot
manifest under `build/structure-review/baseline-5645e811a2f5/`.

## Stage D developer and release-tool result

Contributor-only development helpers now live under `tools/dev/`; release
evidence collection and validation now live under `tools/release/`. The split
preserves executable modes and repository-root resolution while making their
different owners explicit. Playwright full-E2E startup, environment-reference
generation, release acceptance, release-check contracts, and tooling tests all
name the canonical paths.

The installer excludes anchored `/tools`, and the installed-tree residue test
proves that neither developer nor release tooling reaches an operator release.
Source archives continue to contain the tools as required preferred-form source.
CI Bash and ShellCheck discovery now includes `tools/**/*.sh`; tracked-script
parity remains executable rather than relying on documentation alone.

The API error-field audit now lives at `tools/quality/audit_error_fields.py`.
Its location makes the CI-owned quality policy distinct from local development
helpers and release evidence tooling; CI, the executable audit test, the ADR,
and contributor documentation all consume the same canonical path.

## Stage D release-review and host-operations result

The release-readiness gate and its 18-file prompt suite now live under
`tools/release/review/`. Root `plans/` contains no tracked files and remains an
ignored workspace for external evidence. The release-check skill, release plan,
prompt self-references, shell coverage, and repository hygiene tests all name
the canonical review paths. Both prompt-suite scripts retain exact temporary
ShellCheck exemptions for their pre-existing warnings while remaining covered
by CI syntax checks.

All 34 host-operation sources now live under `ops/host/`: 32 executable helpers
and two importable Python policy modules retain their original modes. Backend
runtime path construction, the development launcher, installer and privileged
root configuration, Volatility delegation, sudoers installation, tests, and
operator documentation move atomically. CI Bash and ShellCheck discovery now
includes `ops/**/*.sh`; only the migration matrix and executable topology
contract retain the former source path as historical metadata.

After the host move, the application suite still completes with 2,883 passed
and 17 skipped, and the repository tooling suite completes with 846 passed and
1 skipped. Focused privileged-host, installer/release, and backend/forensics
path suites complete with 259 passed; 312 passed and 1 skipped; and 86 passed,
respectively. Ruff, formatting, Mypy across 357 source files, and the strict
multilingual MkDocs build remain green.

## Stage D image-operations result

The image downloader and its shared size/headroom metadata now live under
`ops/images/`; root `scripts/` contains no tracked files. The downloader retains
its executable mode, resolves the repository root from its additional path
depth, and still places mutable images in the stable installed data root rather
than an immutable release. Installer metadata sourcing, first-VM instructions,
launcher remediation, operator docs, tests, and release guidance all use the
canonical path.

The focused image/provenance suite completes with 47 passed and 1 skipped; the
installer/release selection suite completes with 256 passed and 1 skipped; and
shell/i18n boundary checks complete with 99 passed. The full tooling suite
remains at 846 passed and 1 skipped. Ruff, formatting, Mypy across 357 source
files, and the strict multilingual MkDocs build remain green.

## Stage D systemd-template result

The three installed service templates now live under `ops/systemd/` with their
non-executable modes preserved. Installer preflight, release rendering,
privileged-root reconfiguration, synthetic release layouts, upgrade mutation,
operator references, and unit-policy tests all use the canonical source path.
The former directory remains only as migration metadata.

The complete tooling suite remains at 846 passed and 1 skipped after the move.
Ruff, formatting, Mypy across 357 source files, Bash, ShellCheck, and the strict
multilingual MkDocs build remain green.

## Stage D deployment result

The install, privileged-root configuration, and uninstall implementations now
live under `ops/deploy/`. Three executable wrappers remain under `deploy/` for
one supported compatibility release: execution delegates to the canonical
implementation, while sourcing the install and privileged-root wrappers loads
the canonical function contracts without running them. CI pins the canonical
implementations while recursive shell discovery continues to check both roots.

Repository-root discovery accounts for the additional directory depth.
Installer, launcher, runtime remediation, release/archive policy, operator
documentation, and Python tests use `ops/deploy/`; the real-host upgrade matrix
continues to invoke the compatibility entrypoints deliberately. The staged
source archive contains both the canonical implementations and wrappers with
their executable modes preserved.

After the deployment move, the application suite remains at 2,883 passed and
17 skipped. The pre-commit tooling run completes with 845 passed, 1 skipped,
and the single committed-HEAD archive assertion deselected; the staged-tree
equivalent passes before commit. After commit, that archive assertion passes
and the complete tooling suite returns to 846 passed and 1 skipped. Ruff,
formatting, Mypy across 357 source files, Bash, ShellCheck, and the strict
multilingual MkDocs build remain green.

## Stage E branch baseline

Stage E uses commit `68dd44b3bd07` as its corrected branch baseline. The first
run at the Stage D handoff exposed an intermittent create-if-needed VM race:
resource registration could reject a session that concurrent cleanup had made
terminal, and the route returned HTTP 500 instead of the documented 409
conflict. Commit `b673aed` adds deterministic coverage and narrows the
translation to that registration boundary. Fifty diagnostic stress iterations,
133 focused playground tests, and the full application suite pass after the
repair. Commit `68dd44b` then ratchets the executable collection contract for
the added regression test.

Ignored comparison evidence is stored under
`build/structure-review/baseline-68dd44b3bd07/`. The recorded status is empty,
the top-level manifest contains 43 entries, and default application discovery
collects 2,901 tests. The application suite completes with 2,884 passed and 17
skipped at 86.17% coverage; the separate tooling suite completes with 846
passed and 1 skipped, and combined application coverage remains 86.17%.

The clean-archive baseline wheel contains 159 entries: 144 application entries
and 9 migration entries under `backend/`, no test or frontend entries, and only
the root license plus the compatibility review in distribution license
metadata. Stage E must replace that implicit backend package boundary, add the
required web and migration package data, and complete the ratified license and
notice set without admitting tests or operator data. Ruff, formatting, Mypy,
strict multilingual MkDocs, JavaScript lint and formatting, 26 JavaScript unit
tests, 41 documentation browser tests, and 28 UI smoke tests all pass at the
corrected baseline.

## Stage E result

Stage E replaces the implicit `backend` distribution with the explicit
`src/astinuslab` package, keeps Alembic revisions under
`src/astinuslab/migrations`, and makes templates and static assets package-owned
under `src/astinuslab/web`. The three structural moves landed independently in
commits `b351529`, `e8aac56`, and `cd6dc71`. There is no `backend` import facade:
the internal `backend.app` namespace and the old direct Alembic configuration
path are intentional breaking changes documented with their replacements.

The final wheel has 252 members: 240 package members, including 13 migration
resources, 32 templates, and 49 static files. It contains no `backend/`,
`frontend/`, or `tests/` member. Its eight license entries are `LICENSE`,
`LICENSE_COMPATIBILITY_REVIEW.md`, `THIRD_PARTY_NOTICES.md`, and the five
ratified `LICENSES` manifest/text files. A fresh wheel contract imports public
entry modules, registers stable Celery tasks, initializes a database to the
unchanged `a023_playground_cleanup_exclusion` head, and loads representative
templates and static assets without a repository-root import.

The final runtime matrix uses separate clean archives and virtual environments.
Explicit source, editable, wheel-only, and installer-staged release contexts
each complete with 2,885 passed and 9 skipped; the editable context retains
86.13% application coverage. The wheel context declares the checkout as its
code root for operational resources and a separate data root for mutable
state. That check exposed and fixed two real split-root deviations: Celery's
SQLite broker/result defaults now follow `data_dir`, and generated environment
documentation distinguishes `<base_dir>` from `<data_dir>`.

Final repository gates report 848 tooling tests passed and 2 skipped, Ruff,
formatting, and Mypy across 361 source files green, strict multilingual MkDocs
green, JavaScript lint/format green, 26 JavaScript unit tests, 41 documentation
browser tests, and 28 UI smoke tests passed. Raw context and gate logs remain
ignored under `build/structure-review/`.

## Stage F result

Stage F packages the remaining vertical ownership slices under `src/astinuslab`:
database, API, jobs, orchestration, forensics, and core/platform policy. The
package implementations are canonical; the root module names were removed after
the legacy-code audit's Verdict A on 2026-08-29. All
formerly deferred Python tests now live under explicit domain directories, and
the topology contract rejects new unowned files directly under `tests/python/`.

The application collection contract is 2,885 tests and the combined application
plus tooling collection is 3,772. The full application run completes with 2,868
passed and 17 skipped in the fake-libvirt environment; the tooling run completes
with 885 passed and 2 skipped without fake-mode variables. Domain split runs,
import-boundary checks, Ruff, formatting, and Mypy all pass. The refreshed
code-only Graphify index contains 2,541 nodes and 8,080 raw edges before
clustering, and 2,522 nodes with 6,834 edges after clustering; these are
advisory navigation evidence only.

## Stage G result

Stage G hardens the runtime/configuration boundary without renaming the
development `storage/` root. `ASTINUSLAB_DATA_DIR` remains independent from the
package code root, installed systemd units inject the stable install root, and
mutable state remains outside content-addressed release directories. The
installer resolves and verifies the configured storage root before mutation;
release switching, permission reconciliation, privileged policy publication,
rollback, and uninstall preserve or reverse operator-owned state deliberately.

The focused storage-root agreement, service-switch, upgrade-matrix, release
layout, installer, repository-hygiene, deployment-config, and migration-recovery
contracts pass: 120 boundary tests and 371 installer/release/migration tests,
with 2 expected skips. Git reports only `storage/.gitkeep` under the repository
runtime root. No `var/` rename is attempted; a future data-root rename still
requires a separate journaled migration and rollback plan.

## Stage H result

Stage H removes the three expired root deployment wrappers:
`deploy/install.sh`, `deploy/uninstall.sh`, and
`deploy/configure-privileged-roots.sh`. `ops/deploy/` is now the sole candidate
implementation and the source tree has no tracked files under the former
`deploy/` root. The acceptance matrix resolves installer, uninstaller, and
privileged-root paths from each tree: the current candidate must expose
`ops/deploy`, while a genuine prior archive may still expose its historical
`deploy` layout for the upgrade row.

The removal was intentionally limited to the expired command-path wrappers.
Earlier Stage H documentation said that package root aliases for moved Python
modules remained identity-preserving and documented because they protected
supported import and monkeypatch seams; it also said that removing those aliases
was a separate versioned API decision with its own consumer update matrix.
**Verdict A override (2026-08-29):** that policy was superseded by the
legacy-code audit, and all 35 aliases were removed after their consumers were
repathed. Topology, shell syntax/ShellCheck, install-script, release-layout, and
upgrade-matrix contracts pass after the wrapper removal.
