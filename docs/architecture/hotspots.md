# Codebase Hotspots & Size Budgets

This page lists files and functions that exceed soft size budgets. The
goal is visibility, not a hard policy: large files are tolerated where
the responsibility is genuinely single, but unbounded growth should
trigger a focused extraction before the next non-trivial change.

When you touch any item in this list and the change adds more than ~20
net lines, prefer a small extraction pass that brings the count down.

The numbers below are checked by `tests/python/tooling/test_hotspot_budgets.py`, so
they cannot drift: if a listed item grows, the suite fails until the LOC
column is restated, and a file or function that crosses a discovery
threshold fails until it is added to the table. The soft ceilings are
**not** enforced -- several entries sit over their ceiling by deliberate
choice, and the notes say why.

What *is* enforced is the direction of travel. The same suite records, per
table, how many entries sit over their ceiling, by how many lines in total, and
what the ceilings add up to. All three are restated exactly, and the rule for
restating them is that they may only go down. Raising a ceiling to make an
entry compliant does not work: the ceiling sum moves and the suite says the
table got worse. Cyclomatic complexity has its own ratchet in `pyproject.toml`
(`lint.mccabe.max-complexity`), pinned to the worst function in the tree so
nothing new may be worse than what is already the worst.

## How the numbers are measured

- **File LOC** is the physical line count, as `wc -l` reports it.
- **Function LOC** spans the `def` line through the last line of the
  body. Decorators are excluded, so adding one does not move the number.
- The function scan covers module-level functions and methods of
  module-level classes. Closures nested inside a function are already
  counted inside their enclosing entry.
- **Shell file LOC** is the physical line count of a tracked `*.sh`.
- **Shell embedded Python** is the number of lines inside that script's
  `python3 - ... <<'PY'` heredoc bodies, found by the same discovery
  helper the lint gates use, so the two can never disagree about what
  counts as embedded Python.

## Discovery thresholds

Anything at or over these sizes **must** appear below. The first two
apply to `src/astinuslab`; the last two apply to every tracked `*.sh`.

- Files: **900** lines.
- Functions: **180** lines.
- Shell files: **700** lines.
- Shell embedded Python: **250** lines.

These are looser than the per-entry ceilings on purpose. A ceiling says
where a module should land; a threshold says the point at which staying
off this page stops being an option. Listing something smaller is always
allowed, and a few entries are tracked proactively while still under
their ceiling.

## Baseline gates

Last fully measured 2026-09-05, on `feature/portable-lab-sharing`:

- `.venv/bin/python -m ruff check .`: passing.
- `.venv/bin/python -m ruff format --check .`: passing, 499 files.
- `.venv/bin/python -m mypy src tests/python`: passing under mypy 2.1.0, 455 source files.
- `ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q`:
  passing, 4005 passed, 18 skipped, total coverage 86.02%.
- `.venv/bin/python -m pytest tests/python/tooling -q`: passing, 933 passed,
  2 skipped. Rechecked 2026-09-06 after moving the durable portable-lab-sharing
  design into `docs/plans/`, leaving `docs/superpowers/` tool-local and untracked.

The largest source Python files remain the orchestration / Volatility 3
workflow modules. Re-run the quality gates after touching any listed
module; the active coverage gate is 86% of lines *and branches*.

## Long files

| File | Current LOC | Soft ceiling | Notes |
|------|-------------|--------------|-------|
| `src/astinuslab/i18n_data/en.py` | 975 | 1100 | Locale data, with matching workflow keys and placeholders across all four languages. Keep domain terminology and parity checks when splitting. |
| `src/astinuslab/i18n_data/es.py` | 975 | 1100 | Locale data, with matching workflow keys and placeholders across all four languages. Keep domain terminology and parity checks when splitting. |
| `src/astinuslab/i18n_data/fr.py` | 975 | 1100 | Locale data, with matching workflow keys and placeholders across all four languages. Keep domain terminology and parity checks when splitting. |
| `src/astinuslab/i18n_data/ca.py` | 975 | 1100 | Locale data, with matching workflow keys and placeholders across all four languages. Keep domain terminology and parity checks when splitting. |
| `src/astinuslab/lab_packages/package_service.py` | 1827 | 1000 | Portable package lifecycle facade: import, prepare, launch, reset, teardown, deletion, and reconciliation. Largest new module on the page. The reconcile path and the per-action state machine are the two cohesive pieces worth extracting first. |
| `src/astinuslab/lab_packages/prepare_service.py` | 963 | 900 | Prepare admission, retention leases, execution claims and verified asset ownership. R33 separates shared immutable reuse from exclusive mutation; preserve lock ordering and recovery tests when extracting. |
| `src/astinuslab/lab_packages/verification_service.py` | 1463 | 1000 | Creator-side clean-run verification: session lifecycle, worker fencing, checkpoint evaluation, and cancellation. Fencing and evaluation are separable; keep the lease bookkeeping in one place when splitting. |
| `src/astinuslab/lab_packages/archive.py` | 1270 | 1000 | `.astinuslab` reader and writer plus the archive safety checks: traversal, links, duplicate and case-colliding paths, undeclared entries, and compression bombs. Length here is the threat list; extract the writer before the validators. |
| `src/astinuslab/lab_packages/api.py` | 1523 | 900 | FastAPI routes for the package workflow. Flat route bodies, so split by workflow stage -- inspect/import, prepare, launch, verification -- as it grows. |
| `src/astinuslab/forensics/vol3_support.py` | 1207 | 1000 | Volatility 3 / ISF symbol build in the `forensics/` package. Symbol-build chain is tightly coupled to provider state; extract only with targeted coverage. |
| `src/astinuslab/playground/repositories.py` | 1269 | 900 | Playground persistence: VM, network, and NIC row lifecycle plus the compare-and-swap claim helpers. Mostly flat query functions, so growth is cheap to split by entity. |
| `src/astinuslab/db/models.py` | 1163 | 1200 | Declarative table definitions for the whole schema; grew by the package, instance, and verification tables. Growth is one class per table rather than tangled logic, so the generous ceiling is deliberate. Split by domain only if the file stops being readable top to bottom. |
| `src/astinuslab/catalog.py` | 1096 | 900 | Catalog loading, compatibility selectors, and image-discovery inference. Keep additions narrow or extract cohesive parser/normalization helpers. |
| `src/astinuslab/jobs/package_instance_coordinator.py` | 1518 | 900 | Celery-side coordinator for package instance actions: reservation, locked boot, overlay allocation, and stale-claim cleanup. Cleanup and reservation both take locks; extract them together or not at all. |
| `src/astinuslab/storage/publication.py` | 1070 | 900 | Artifact publication, hashing, and manifest writing. Split by artifact kind if it grows again. |
| `src/astinuslab/orchestration/deps.py` | 777 | 1500 | Explicit dependency container that replaced the dissolved `orchestrator.py` re-export shim; mostly default wiring closures. Deliberately generous ceiling -- see the `JobRunDeps.default` note below. |
| `src/astinuslab/db/repositories.py` | 1033 | 900 | Persistence queries and lifecycle guards; bounded collection queries and grouped acquisition counts added by the backend audit. Split by entity before further growth. |

## Long functions

| Function | LOC | Soft ceiling | Notes |
|----------|-----|--------------|-------|
| `src/astinuslab/orchestration/deps.py::JobRunDeps.default` | 623 | 600 | The production wiring block: one closure per injected dependency. Cyclomatic complexity is 1, so length here is a property of the explicit-wiring pattern rather than tangled logic. Treat *growth* as the signal, not the absolute number; if it passes the ceiling, split the container by domain instead of shortening the closures. |
| `src/astinuslab/orchestration/runners/boot_capture_runner.py::boot_and_prepare_windows` | 235 | 180 | Windows preflight, per-instance firmware, QGA bootstrap, and cleanup. Keep phase ordering explicit; extract only with boot-path tests. |
| `src/astinuslab/orchestration/runners/boot_capture_runner.py::boot_and_prepare` | 210 | 180 | Platform dispatch and Linux boot preparation. Preserve the platform boundary and failure cleanup when extracting. |
| `src/astinuslab/forensics/vol3_analysis.py::_run_reserved_analysis` | 417 | 180 | Higher regression risk than the job-run flow modules. |
| `src/astinuslab/forensics/capture_support.py::_acquire_and_publish_locked` | 320 | 180 | Holds capture-side lock semantics. |
| `src/astinuslab/forensics/vol3_support.py::run_vol3_symbol_build` | 280 | 180 | Symbol-build chain is tightly coupled to provider state. |
| `src/astinuslab/jobs/startup_recovery.py::cleanup_stale_playground_resources` | 266 | 180 | Startup sweep with ownership revalidation across libvirt, filesystem, and database cleanup. Keeps package resources, frozen verification, and capture custody intact; extract one stage at a time only with targeted custody coverage. |
| `src/astinuslab/forensics/vol3_plugin_run.py::run_vol3_plugin` | 395 | 180 | Plugin invocation, ISF selection, and result persistence in one body. |
| `src/astinuslab/lab_packages/package_service.py::LabPackageService.reconcile` | 237 | 180 | Startup reconciliation for package instances, content leases, and interrupted actions. Same hazard class as `reconcile_libvirt_resources`: a bug here leaks storage and VMs, so extract one stage at a time behind targeted coverage. |
| `src/astinuslab/playground/vm_lifecycle.py::spawn_vm` | 190 | 180 | Down from 416, and unchanged by the domain split: it moved modules, it was not cut further. What is left is the try/except/finally skeleton that owns the claim lease and the failure cleanup, plus the calls to the eight phases split out of it. Each further cut would add a call site about as long as the block it removes. |
| `src/astinuslab/lab_packages/export_service.py::export_locked` | 230 | 180 | Locked export: attestation check, definition freeze, layer digesting, and archive assembly. The digest and assembly half is the cleaner extraction. |
| `src/astinuslab/db/__init__.py::restore_database` | 209 | 180 | Backup restore with schema checks and rollback. Branch-heavy by nature; keep the rollback path in one place when splitting. |
| `src/astinuslab/lab_packages/verification_service.py::_run_verification` | 205 | 180 | One clean-run verification attempt end to end. Branch-heavy because every stage carries a cancellation and a failure exit. |
| `src/astinuslab/lab_packages/archive.py::write_archive_to` | 201 | 180 | Archive writer: member ordering, canonical manifest, and payload streaming in one pass so the output stays byte-deterministic. Splitting it risks the determinism it exists to guarantee. |
| `src/astinuslab/lab_packages/archive.py::inspect_archive` | 201 | 180 | The passive inspection path: every archive safety check that runs before anything is imported. Length is the threat list; keep the checks in one readable sequence. |
| `src/astinuslab/lab_packages/prepare_service.py::run_prepare` | 41 | 180 | Content acquisition: embedded bytes, then the local cache, then explicitly pinned sources, with atomic publication and resumption. |
| `src/astinuslab/virtualization/process_support.py::run_command` | 221 | 180 | Bounded process supervision and exception-preserving cleanup; keep custody transitions together. |
| `src/astinuslab/lab_packages/launch_service.py::launch_package` | 197 | 180 | Transactional launch: preflight recompute, reservation, overlay creation, and rollback on any failure. Only just over the ceiling; keep the rollback path whole. |
| `src/astinuslab/jobs/runtime_support.py::parse_guest_ip` | 156 | 150 | Guest-network parsing has many fallback branches. Keep changes narrow and backed by compatibility tests. |

## Long shell scripts

Shell is the project's least-checked surface. ShellCheck and `bash -n`
catch syntax and quoting; nothing reads these files for logic, there is
no coverage measurement, and the one executable acceptance harness --
`tests/shell/host/upgrade_matrix.sh` -- still needs a disposable host and a
human to start it. It is now dispatchable as
`.github/workflows/upgrade-acceptance.yml` rather than assembled by hand, but
it is deliberately not scheduled: it installs and uninstalls for real, as root,
so it runs where someone has declared a host expendable. Both release-blocking defects found by
host acceptance were in `ops/deploy/install.sh`, and one of them was an
unanchored `rsync --exclude` pattern. Size is the only cheap proxy for
that risk, so it is budgeted here like the Python surface.

| Script | Current LOC | Soft ceiling | Notes |
|--------|-------------|--------------|-------|
| `ops/deploy/install.sh` | 3590 | 2500 | Still the highest-consequence file in the repository: it runs as root, creates identities, moves ACLs and MAC policy, and commits or rolls back an upgrade. 136 functions, so length is composition rather than one blob. Down from 5151 now that the ACL snapshot/restore, traversal-manifest, transaction-log, SSH-key, managed-config, permissions, config-authoring, identity-marker and legacy-cleanup bodies are real modules. Reduce it further by moving heredoc bodies out (see the next table), not by merging shell functions. |
| `tests/shell/host/upgrade_matrix.sh` | 1374 | 1200 | The install/upgrade/rollback acceptance matrix. Length is the row count and the evidence each row records; splitting it would split the single report a release reviewer reads. Budgeted so growth stays deliberate. |
| `astinuslab.sh` | 1211 | 900 | Operator launcher and `setup-host`. Mixes dev-mode service control with host preparation; the host-preparation half is the separable one. |
| `ops/deploy/configure-privileged-roots.sh` | 782 | 700 | Privileged storage-root configuration, with a transaction log and exact rollback. The rollback itself lives in `ops/deploy/host_access.py`, which this script and the installer both call, and the identity-marker check in `ops/deploy/privileged_state.py`, which all three privileged entry points call. 35% of what is left is still embedded Python. |

## Embedded Python in shell

1,581 lines of Python live inside `<<'PY'` heredocs. Ruff, Mypy and
coverage all key on `*.py`, so none of them sees this code; until the
discovery helper was widened for backslash continuations, 791 of those
lines were not even syntax-checked. `tests/python/tooling/test_host_scripts.py`
now compiles every body and runs pyflakes over it, but that is a floor,
not type checking and not coverage. Treat a growing number here as a
signal to ship the logic as a real module under `ops/` and call it,
rather than as a reason to raise the ceiling.

2,419 lines have gone that way already: `ops/deploy/acl_snapshot.py`,
`ops/deploy/host_access.py`, `ops/deploy/ssh_key.py`,
`ops/deploy/managed_config.py`, `ops/deploy/permissions.py` and
`ops/deploy/privileged_state.py` are imported by nothing and invoked as
subprocesses, exactly as the heredocs were, but Ruff, Mypy and their own unit
tests now read them. `ops/deploy/uninstall.sh` left both tables that way: it
went from 819 lines and 480 of embedded Python to 349 and none.

| Script | Embedded Python LOC | Soft ceiling | Notes |
|--------|---------------------|--------------|-------|
| `ops/deploy/install.sh::embedded-python` | 677 | 800 | Down from 2333: the ACL snapshot/restore, traversal-manifest, transaction-log, SSH-key, managed-config, permissions, config-authoring, identity-marker and legacy vol3 cleanup bodies are now `ops/deploy/acl_snapshot.py`, `ops/deploy/host_access.py`, `ops/deploy/ssh_key.py`, `ops/deploy/managed_config.py`, `ops/deploy/permissions.py` and `ops/deploy/privileged_state.py`. What is left is a long tail with no single large body. |
| `ops/deploy/configure-privileged-roots.sh::embedded-python` | 276 | 300 | Under its ceiling since the 281-line rollback body became `ops/deploy/host_access.py`, shared with the installer that writes the records it reads, and the identity-marker check became `ops/deploy/privileged_state.py`. The 92-line install-source authority check is what remains. |

## What to do when this list grows

1. Pick the smallest item over its ceiling.
2. Extract the longest single-responsibility contiguous block into a
   sibling helper in the same module (or a sibling `_helpers.py` if a
   clear theme emerges).
3. Helper must take all inputs as parameters and return values
   explicitly. No shared mutable state.
4. Run the targeted test slice for the touched module, then full
   `ASTINUSLAB_FAKE_LIBVIRT=1 pytest -q`.
5. Update the LOC numbers in this file in the same commit.
   `tests/python/tooling/test_hotspot_budgets.py` fails until you do.

For a shell entry, step 2 is different: the extraction target is a real
`*.py` module under `ops/`, invoked as a subprocess, not another shell
function. That is what moves the code under Ruff, Mypy and coverage.
