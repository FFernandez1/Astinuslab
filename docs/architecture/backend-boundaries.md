# Backend boundaries

AstinusLab backend should move toward vertical packages for workflows instead of adding more flat `src/astinuslab/*_support.py` modules.

## Current package seams

`astinuslab.live_session` is the vertical package boundary for live-session (manual-trigger) lab execution.

- `src/astinuslab/live_session/__init__.py` keeps the package entrypoint `run_live_session_job`.
- `src/astinuslab/live_session/runner.py` owns the live-session run flow.
- `src/astinuslab/live_session/support.py` owns live-session helpers: attacker boot parallelisation, workload provisioning, capture handoff.

`astinuslab.forensics` is the vertical package boundary for memory capture and Volatility 3 / ISF symbol analysis.

- `src/astinuslab/forensics/capture_support.py` owns `acquire_and_publish` capture + artifact promotion; `memory_dump.py` wraps the libvirt core dump.
- `src/astinuslab/forensics/vol3_support.py` and `src/astinuslab/forensics/vol3_analysis.py` own Volatility 3 analysis; `forensics/symbols/` owns ISF symbol providers and build.

`astinuslab.playground` is the established vertical package boundary for Playground lab workflows.

- `src/astinuslab/playground/api.py` assembles the Playground FastAPI router and shared route helpers.
- `src/astinuslab/playground/routes/*.py` own grouped Playground route handlers while preserving the `/api/v1/playground` API surface.
- `src/astinuslab/playground/orchestrator.py` owns Playground Celery tasks and VM/network/capture orchestration.
- `src/astinuslab/playground/repositories.py` owns Playground persistence helpers.
- `src/astinuslab/playground/recipes.py` owns current JSON Playground recipe import/export validation.
- Legacy root-level `src/astinuslab/playground_*.py` facades have been removed after caller verification. New behavior belongs in the package modules, and reconciliation now lives at `src/astinuslab/playground/reconciliation.py`.

`astinuslab.orchestration` is the vertical package boundary for scenario-driven job runners.

- `src/astinuslab/orchestration/runners/boot_capture_runner.py` owns the scenario job-run flow (boot, acquisition, post-script), wired through `job_run_deps.py`.
- `src/astinuslab/orchestration/runners/isf_matrix_runner.py` owns batch ISF matrix runs across target images.

`astinuslab.virtualization` is the vertical package boundary for libvirt/KVM and guest plumbing.

- `src/astinuslab/virtualization/vm_lifecycle.py` owns libvirt domain, network, disk, and VM lifecycle helpers.
- `src/astinuslab/virtualization/cloud_init.py`, `boot_staging.py`, and `guest_wait.py` own cloud-init seed generation, boot staging, and QGA/SSH guest-readiness waits.
- `src/astinuslab/virtualization/vm_support.py`, `process_support.py`, `host_helpers.py`, `host_script_manifest.py`, `guest_script_support.py`, and `entrypoint_support.py` own supporting helpers for VM XML, host processes, and host/guest scripts.

`astinuslab.storage` is the vertical package boundary for artifact and storage management.

- `src/astinuslab/storage/artifacts.py` owns artifact manifests (`artifact-manifest/v1`) and SHA-256 sidecars.
- `src/astinuslab/storage/retention.py` owns retention sweeps; `keystore.py` owns the SSH keystore; `run_path_support.py` and `storage_admin_support.py` own run-path resolution and the Storage admin page support.

`astinuslab.images` is the vertical package boundary for VM image administration.

- `src/astinuslab/images/image_admin_support.py` owns image upload/admin operations; `image_inspection.py` owns image metadata inspection.

`astinuslab.workloads` is the vertical package boundary for workload provisioning support.

- `src/astinuslab/workloads/workload_support.py` owns workload install helpers; `workload_cache_support.py` owns the workload package cache.

The Celery task name `run_manual_trigger_job` and the `manual_trigger` execution-mode value are preserved as the stable wire contract even though the implementation now lives in `live_session`.

Future splits should follow the same shape: create the package first, move implementation into package modules, and preserve task names, URLs, and monkeypatch surfaces.

## Stage F flat-module ownership

The table below is the ownership decision for every Python module that remained
directly under `src/astinuslab/` at the Stage F baseline (`86494f0`). "Imports
out" records the significant internal dependencies; "imports in" records the
principal consumers rather than repeating every leaf import. The test column is
the executable protection used when moving the implementation. **Root alias
status (2026-08-29):** Verdict A of the legacy-code audit removed all 35 root
compatibility aliases after callers were repathed; the destinations below are canonical.

| Baseline module | Owner / destination | Imports out | Imports in | Protecting tests |
| --- | --- | --- | --- | --- |
| `__init__.py` | cross-cutting package bootstrap facade | `fake_mode` | package importers, worker/API startup | `test_fake_mode.py`, `forensics/test_fake_libvirt_guard.py`, installed-artifact tests |
| `api.py` | API/web → `api/__init__.py` | case/job/forensic services, DB session, schemas, Playground recipe routes | `main` | `api/test_api.py`, `api/test_api_v1_extended.py`, `api/test_openapi_contract.py` |
| `api_errors.py` | API/web → `api/errors.py` | FastAPI responses | API, services, Playground and UI adapters | API tests, `test_error_field_audit.py`, service-domain tests |
| `app_settings.py` | core/platform settings facade; implementation is DB-backed | DB session, settings repository, storage retention defaults | readiness, retention, settings UI, Celery | `test_app_settings.py`, `ui/test_settings_routes.py`, `test_retention.py` |
| `boot.py` | orchestration runner entrypoint | boot/capture runner | Celery assembly, ISF matrix runner | `test_lab_orchestrator.py`, `test_runner_kwargs_only.py`, Celery tests |
| `case_services.py` | forensics/evidence services → `forensics/case_services.py` | catalog, repositories, API schemas | API | `forensics/test_acquisitions.py`, `test_artifacts.py`, `test_cve.py`, API tests |
| `catalog.py` | justified cross-cutting catalog facade → core/platform catalog implementation | compatibility policy, config, image inspection | API, jobs, images, forensics, Playground, UI, workloads | `test_catalog.py`, `test_catalog_contract.py`, `playground/test_catalog.py`, compatibility/CVE tests |
| `celery_app.py` | jobs composition root → `jobs/celery_app.py` | workflows, DB, services, retention, reconciliation | worker entrypoints, job control/services, Playground | `test_celery_app.py`, `forensics/test_celery_isf_behavior.py`, task-name contract |
| `celery_main.py` | jobs worker entrypoint | Celery app/signals, Playground task registration | launcher and systemd worker commands | `test_celery_app.py`, `test_celery_worker.py`, installed-artifact tests |
| `celery_worker.py` | jobs compatibility worker adapter | Celery app | compatibility imports | `test_celery_worker.py` |
| `celery_worker_signals.py` | jobs worker lifecycle → `jobs/celery_worker_signals.py` | config, readiness, recovery, DB, virtualization | Celery worker entrypoint | `test_celery_worker.py`, readiness/startup-recovery tests |
| `compatibility.py` | core/platform catalog policy → `core/compatibility.py` | standard library only | catalog, orchestration deps, Playground | `test_compatibility.py`, `test_catalog_contract.py`, `test_multidistro.py` |
| `config.py` | core/platform configuration → `core/config.py` | standard library/Pydantic settings | nearly every domain and composition root | config tests, installed-artifact and entrypoint tests |
| `csrf.py` | API/web request adapter → `api/csrf.py` | API errors | main, security, UI | `test_security_auth.py`, API/UI auth tests |
| `db.py` | database package entrypoint → `db/__init__.py` | core config, frozen baseline | every persistence consumer, migrations, CLI | `test_db_*.py`, `test_alembic.py`, migration tests |
| `db_baseline_snapshot.py` | database migration support → `db/baseline_snapshot.py` | none | DB bootstrap only | `test_db_schema.py`, `test_db_migration.py`, Alembic tests |
| `dbcli.py` | database CLI adapter | DB package, entrypoint error formatter | console script | `test_db_backup*.py`, `test_entrypoint_errors.py`, installed-artifact tests |
| `fake_mode.py` | core/platform test-double policy → `core/fake_mode.py` | environment only | bootstrap, health, storage, forensics, virtualization | `test_fake_mode.py`, fake-libvirt guard tests |
| `forensic_services.py` | forensics → `forensics/services.py` | forensics tools/runners, jobs, locks, repositories | API, Celery, UI, Volatility API | forensics analysis/plugin/capture tests, UI Volatility tests |
| `i18n.py` | API/web presentation facade | `i18n_data` | UI | `test_i18n_parity.py`, `test_i18n_split.py`, UI tests |
| `job_control.py` | jobs → `jobs/job_control.py` | Celery composition, config/DB, job states/repository, process adapter | job services, workflows, UI | `jobs/test_cancellable_states.py`, job PID/cancellation tests |
| `job_run_context.py` | orchestration → `orchestration/context.py` | orchestration dependencies | live-session and boot/capture runners | runner and live-session tests |
| `job_run_deps.py` | orchestration dependency wiring → `orchestration/deps.py` | catalog/config/DB/jobs plus storage/virtualization/workload adapters | orchestration runners, live session, runtime control | `test_lab_orchestrator.py`, `test_runner_kwargs_only.py`, runtime tests |
| `job_services.py` | jobs → `jobs/job_services.py` | API error/schema compatibility seams, catalog, Celery, DB/repository, storage | API, UI, forensics | jobs/API tests, `test_lab_api.py`, runtime tests |
| `job_state_support.py` | jobs → `jobs/job_state_support.py` | injected DB/repository/process seams | orchestration runners | `jobs/test_job_state_support.py`, runner tests |
| `job_status.py` | jobs → `jobs/job_status.py` | none | repositories, services, storage, forensics | `test_job_status*.py`, `jobs/test_job_transitions.py` |
| `lab_lifecycle.py` | orchestration → `orchestration/lab_lifecycle.py` | jobs, DB/repository, runtime support, virtualization | Celery, orchestration dependencies/runners, Playground | `test_vm_lifecycle.py`, `test_integration_lifecycle.py`, lab/Playground tests |
| `locks.py` | core/platform locking policy → `core/locks.py` | host ACL helper | forensics, Celery, Playground | blocking/capture-lock and concurrency tests |
| `logging_config.py` | core/platform logging → `core/logging_config.py` | request context | API and worker composition roots | request-observability/context tests |
| `main.py` | API/web application composition root | API routers, core policy/health, UI/web resources | ASGI entrypoint | API/OpenAPI/readiness/security/UI tests, smoke tests |
| `models.py` | database → `db/models.py` | SQLAlchemy `Base` | repository/storage/Playground/jobs adapters, Alembic metadata | `test_db_schema.py`, `test_alembic.py`, repository/storage/Playground tests |
| `operational_health.py` | core/platform health → `core/operational_health.py` | config and DB engine probes | API composition root | `test_operational_health.py`, API readiness tests |
| `orchestration_support.py` | orchestration → `orchestration/support.py` | injected adapters only | lab lifecycle, runners, live session | `test_orchestration_support.py`, runner/live-session tests |
| `output_parsers.py` | core/platform process-output policy → `core/output_parsers.py` | standard library only | boot/capture runner | `test_output_parsers.py`, runner tests |
| `playground_constants.py` | Playground → `playground/constants.py` | none | Playground network routes | Playground network/API tests |
| `readiness.py` | cross-cutting API composition facade | core settings/health, jobs broker, DB, forensics, virtualization | API and worker readiness | `api/test_readiness.py`, `test_celery_worker.py`, operational-health tests |
| `repositories.py` | database → `db/repositories.py` | models, job status, temporary API `JobCreate` typing seam | all persistence consumers | `jobs/test_repositories.py`, Playground repository tests, DB/service tests |
| `request_context.py` | core/platform request observability → `core/request_context.py` | standard library only | logging and API composition | `test_request_context.py`, `test_request_observability.py` |
| `runtime_actions.py` | jobs runtime → `jobs/runtime_actions.py` | injected dependencies | runtime control | `test_runtime_actions.py`, `jobs/test_runtimes.py` |
| `runtime_control.py` | jobs runtime → `jobs/runtime_control.py` | DB/repository, orchestration dependency container, runtime actions | Celery and job services | `test_runtime_control.py`, `jobs/test_runtimes.py` |
| `runtime_support.py` | jobs runtime records → `jobs/runtime_support.py` | injected process/persistence seams | worker signals, orchestration deps/lifecycle | `test_runtime_support.py`, runtime and lifecycle tests |
| `schemas.py` | API/web contracts → `api/schemas.py` | Pydantic and path policy | API plus current typed service/repository seams and vertical adapters | `test_schemas_cidr.py`, `playground/test_schemas.py`, API/OpenAPI tests |
| `security.py` | core/platform security policy → `core/security.py` | config, CSRF adapter | API/UI | `test_security_auth.py`, privileged-boundary and API tests |
| `serve.py` | API/web process entrypoint | entrypoint error formatter | console script | `test_entrypoint_errors.py`, installed-artifact tests |
| `startup_recovery.py` | jobs worker recovery → `jobs/startup_recovery.py` | DB/models, storage publication, Playground cleanup, config | worker signals | `test_startup_recovery.py`, `playground/test_startup_recovery.py`, publication tests |
| `system_status.py` | core/platform read-only host status → `core/system_status.py` | fake-mode policy, subprocess | settings UI | `test_system_status.py`, UI settings tests |
| `volatility_api.py` | forensics HTTP adapter → `forensics/api.py` | forensics services/catalog/readiness, DB/repositories | API composition and UI compatibility | `ui/test_volatility_workbench.py`, Volatility API/forensics tests |

Graphify's code-only communities support these ownership decisions: API route
handlers and API schemas cluster separately (communities 5 and 8), SQLAlchemy
models and DB bootstrap form communities 18 and 20, orchestration context/deps
form community 7, lab lifecycle forms community 24, forensic enqueue services
and the Volatility HTTP adapter form communities 33 and 46, and catalog loading,
image inference, compatibility, and catalog editing form communities 1, 42, 44,
and 76. The main cross-community bridges are `session_scope`, `JobRepository`,
`ApiError`, and `JobCreate`; those are staged compatibility seams, not evidence
that their source modules have one shared responsibility.

Git co-change reinforces the vertical seams. Recent Volatility changes co-change
`forensic_services.py`/`volatility_api.py` with `forensics/vol3_*`; storage
custody changes co-change `models.py` with storage publication/deletion; and
Playground transition changes co-change `models.py` with Playground repositories
and routes. Therefore models remain database metadata, while the behavior stays
in the vertical packages. `catalog.py` co-changes with storage paths, images,
workloads, orchestration, and UI, so it is retained as a documented
cross-cutting platform facade instead of being forced into any one workflow.

### Stage F progress

The API, database, jobs, orchestration, forensics, and core/platform slices have
now landed as separate commits. Each
slice moved implementation into its owning package, kept a root identity alias
where the import path is a supported monkeypatch or operator seam, and added a
package-boundary contract before the move. The jobs slice specifically freezes
all registered Celery task names and verifies that API, jobs, worker, and
startup-recovery tests continue to exercise the package-owned implementation.
The orchestration slice additionally updates runner and live-session imports to
canonical package paths while preserving the dependency-injection types and
legacy monkeypatch imports. The forensics slice does the same for case/evidence
services and the Volatility router, preserving public route identity and
artifact/task contracts. The core/platform slice now packages the ten
cross-cutting policy modules, corrects the moved configuration base path, and
updates all internal consumers to canonical `core` imports while preserving
identity aliases. All formerly deferred root tests are now assigned to explicit
domain, integration, host, or tooling directories. At the Stage F handoff, the
remaining work was dependency-direction ratification and the complete quality
gate. Stage G then verified the
runtime/configuration boundary: mutable data remains outside the versioned
release, the configured storage root agrees with installer and systemd policy,
and upgrade/rollback permissions and custody are covered. Stage H removed the
expired deployment wrappers, updated the historical acceptance matrix to resolve
canonical candidate paths, and completed the full quality gate.

### Ordered move sequence

1. Package API contracts and router assembly while preserving route identity and
   root schema/error aliases.
2. Package SQLAlchemy metadata, DB bootstrap, and repositories; preserve root
   aliases for Alembic and monkeypatch consumers.
3. Package job state/services/runtime/Celery ownership; assert stable Celery task
   names before moving implementation.
4. Move orchestration dependency wiring, run context, shared lifecycle, and
   support helpers into `orchestration`.
5. Move forensic/evidence services and the Volatility router into `forensics`.
6. Move the proven platform-policy modules into `core`, retaining the catalog,
   readiness, and application-settings facades where their composition role is
   intentionally cross-cutting. **Complete.**
7. Assign the remaining root tests to their production domains, ratify import
   directions, refresh Graphify/hotspots, and run installed-artifact plus full
   quality gates. **Test ownership complete; final quality gate in progress.**

## Refactor rules

1. Preserve public import paths with facades during first split.
2. Preserve task names, URLs, template names, and monkeypatch surfaces.
3. Move behavior without semantic cleanup in same commit.
4. Run compile checks and focused compatibility tests after each package split.
5. Only remove facade modules after search proves no callers use them.

## Schema migrations

Schema history is managed by Alembic (`src/astinuslab/migrations/`), with a baseline revision capturing the post-PlaygroundSession-split schema. `initialize_database()` routes on startup:

- fresh database → `create_all` + stamp head;
- pre-Alembic database → the frozen legacy bootstrap in `src/astinuslab/db/__init__.py` (`_migrate_data()` + `_migrate_schema()`) ports it to the baseline schema, then stamps;
- stamped database → `alembic upgrade head`.

New schema changes are Alembic revisions (`alembic -c src/astinuslab/migrations/alembic.ini revision`), not edits to the legacy bootstrap, which is frozen at the baseline. The old `backend/alembic.ini` path is intentionally removed by the Stage E package-layout migration; operators and contributors must use the package-owned path or `python -m astinuslab.dbcli init`.
