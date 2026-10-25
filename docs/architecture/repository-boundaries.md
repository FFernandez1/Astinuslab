# Repository boundaries

This document defines the allowed dependency directions for the target repository. It complements the exact old-to-new mappings in [Repository path migration matrix](path-migration-matrix.md).

## Dependency direction

```text
API and web adapters
        ↓
domain services and workflow packages
        ↓
core policy and explicit platform interfaces
        ↓
database, storage, virtualization, process, and host adapters

operations/tools → documented application and release interfaces
application loaders → shipped config/guest/image/Volatility resources
tests → public interfaces or explicit test-only seams
application/tools → mutable runtime and generated output
```

Dependencies must not point upward. Mutable runtime/generated directories are never import roots. Operations and tools may invoke application entry points, but production application modules must not import contributor/release tooling.

## Installable Python package

The target package is `src/astinuslab/`. Its initial vertical boundaries follow proven current seams rather than inventing a new domain model:

| Package | Responsibility | May depend on |
| --- | --- | --- |
| `api` | FastAPI assembly, HTTP schemas/errors, request/CSRF/auth adapters | domain/workflows, core, web presentation helpers |
| `core` | configuration contracts, logging, health/readiness, security policy, locks, request context | standard library and narrow platform abstractions |
| `db` | SQLAlchemy models, repositories, DB initialization and migration integration | core configuration; no web/template dependency |
| `jobs` | job states, services, control, runtime records/actions | db, core, explicit workflow/platform interfaces |
| `forensics` | capture publication, Volatility analysis, symbol providers/build | jobs, storage, virtualization/process adapters, core |
| `images` | image inspection, metadata, administration, safe download | core, storage/path policy, process adapters |
| `live_session` | manual-trigger workflow | jobs, workloads, virtualization, storage, core |
| `lab_packages` | archive trust, immutable content, definitions, Prepare/preflight/verification and instance intents | db, storage, core; Jobs coordinator and Playground platform adapters execute lab operations |
| `orchestration` | scenario runners and dependency wiring | jobs and vertical workflow/platform packages |
| `playground` | Playground routes/services/repositories/orchestration | api adapters, db, virtualization, storage, core |
| `storage` | artifact manifests/publication, retention, SSH keys, run paths | core and db interfaces |
| `ui` | HTML routes and view composition | domain read/service interfaces, API schemas, web presentation helpers |
| `virtualization` | libvirt/VM lifecycle, cloud-init, guest readiness, host helper invocation | core, resources, process adapters |
| `workloads` | workload provisioning and cache | core, storage, virtualization/guest interfaces |
| `web` | Jinja templates, static assets, view-only helpers | API/view contracts; no database writes or orchestration |
| `migrations` | Alembic environment and immutable revision history | db metadata/config only |

The current flat modules are assigned during the domain-ownership stage using imports, co-change history, and test ownership. A file moves only when its responsibility is clear; file size alone is insufficient.

## Allowed assembly exceptions

Some framework assembly points legitimately know many packages:

- FastAPI application/router assembly registers vertical routers and exception handlers.
- Celery initialization imports task modules so stable task names enter the registry.
- SQLAlchemy/Alembic initialization imports model metadata for migration discovery.
- orchestration dependency wiring composes vertical services and platform adapters.
- Jinja/static application setup resolves package-owned web resources.

These modules are composition roots. They may import downward across several boundaries. Domain packages must not import `main` or application wiring. Task entrypoint modules may import the Celery app to register stable task names; that narrow registration dependency does not authorize importing unrelated application assembly.

Playground execution and reconciliation share `playground.resource_custody` for
state-and-token lease renewal. Every caller supplies its current Session factory
and grace period; this module has no Celery, orchestrator, host or global-settings
dependency. Cleanup and reconciliation phases retain their existing owners.
Jobs admission, capacity and cancellation use `db.transactions.reserve_sqlite_write`
directly, preserving the caller's transaction and guarded-read ordering.

### Existing transaction-boundary exception

`db.repositories.JobRepository.compensate_dispatch_failure` calls
`jobs.capacity.confirm_cleanup` when a queued job never started. This upward
dependency is an existing, narrow exception: action failure, parent failure and
release of pending capacity must share the repository transaction. Its compare
and update conditions preserve work already claimed by a worker. The dispatch
recovery tests cover rejected publication, ambiguous acknowledgement and late
delivery; this exception does not permit general database-to-workflow imports.

The jobs/persistence maintainer owns this coupling. Revisit it when changing
dispatch compensation or capacity ownership, preserving the transaction and
regression checks before extracting a shared policy interface. No broader
dependency refactor is required for unrelated work.

## Test boundaries

The canonical test root is `tests/`:

```text
tests/
├── python/
│   ├── api/
│   ├── core/
│   ├── db/
│   ├── e2e/
│   ├── jobs/
│   ├── forensics/
│   ├── images/
│   ├── integration/
│   ├── live_session/
│   ├── orchestration/
│   ├── playground/
│   ├── storage/
│   ├── virtualization/
│   ├── workloads/
│   ├── host/
│   ├── host_real/
│   ├── ui/
│   └── tooling/
├── browser/
│   ├── unit/
│   ├── smoke/
│   ├── integration/
│   ├── full/
│   ├── docs/
│   ├── helpers/
│   └── fixtures/
├── shell/
│   └── host/
└── fixtures/             only genuinely cross-suite fixtures
```

Domain tests follow the production boundary they protect. The `web` package maps to `tests/python/ui`, migrations map to `tests/python/integration`, and backend-owned end-to-end tests map to `tests/python/e2e`. Cross-cutting contract tests live under `integration` or `tooling`, never at the root. Stage F assigned the formerly deferred root tests to API, core, database, forensics, host, images, jobs, orchestration, Playground, storage, UI, virtualization, workloads, integration, or tooling directories. `host_real` remains opt-in and cannot be counted as passing without its real libvirt/Volatility prerequisites.

Tests may use public interfaces and explicit test-only fixtures/fakes. Monkeypatch strings are migration consumers and must be updated mechanically with import moves. Production modules never import from `tests`.

## Operations, tools, resources, and runtime

- `ops/` contains code that installs, configures, or runs on the application host and is included in installed releases. `ops/host` is the privileged/libvirt boundary; `ops/deploy` owns install/upgrade/uninstall; `ops/images` owns operator-run image provisioning; `ops/systemd` owns unit templates.
- `tools/` contains contributor, quality, and release-process utilities. It is excluded from installed releases, and production application code cannot import it.
- `guest/` remains a first-class boundary because its code executes inside VMs under a different trust and platform model.
- `config/` remains the operator-facing configuration/example contract.
- `images/` remains the image source/provenance and local-image boundary; tracked metadata is source, image bytes are operator state.
- `vol3/` remains visible as a licensing and integration boundary. AstinusLab sources may ship; generated symbols are mutable state; Volatility 3 and dwarf2json remain operator-owned outside the repository/release.
- `storage/` remains the development runtime default. Installed deployments use explicit absolute data roots. No package or tool treats runtime directories as source.

## Documentation and development records

- `docs/` is canonical documentation source. `site/` is reproducible output.
- design/implementation plans, review prompts and release evidence are maintainer records kept outside the public repository; useful conclusions move into normal documentation.
- local agent state remains ignored. Durable conclusions move to normal documentation; generated agent reports are not product source.

## Stable external contracts

Internal folder and import changes do not authorize changes to HTTP routes, Celery task names, Alembic revision IDs, systemd unit names, configuration keys, catalog schemas, artifact formats, or public static URLs. Each has independent compatibility and migration requirements.

## Enforcement

Use existing tools first:

- setuptools package manifests for wheel contents;
- Ruff/Mypy and a focused import-boundary check for Python edges;
- pytest collection/coverage for suite parity;
- ESLint/Prettier and file-match parity for web assets;
- tracked-script enumeration plus Bash/ShellCheck for shell coverage;
- strict MkDocs/i18n/browser tests for docs;
- installer/release-manifest tests for shipped resources;
- Git tracked-file policy for generated/runtime/large artifacts and licenses.

Graphify is advisory navigation evidence. The refreshed Stage F code-only AST
index covers 196 production Python files, 2,541 extracted nodes, and 8,080
edges before clustering (2,522 nodes and 6,834 edges in the clustered graph).
Whole-repository semantic extraction requires an unavailable API key, so
Graphify is neither a gate nor the sole source of a boundary decision.
