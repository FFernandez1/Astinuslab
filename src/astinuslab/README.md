# Backend

Python 3.12 · FastAPI · Celery · SQLAlchemy · SQLite · libvirt

---

## Architecture overview

```
HTTP request
    │
    ▼
FastAPI (main.py / api/ / playground/api.py / ui/)
    │  thin routers — validate, call domain service modules
    ▼
jobs/job_services.py / forensics/services.py / forensics/case_services.py / playground/*
    │  business logic — auth checks, catalog look-ups, job creation, capture, cases
    ▼
db/repositories.py + playground/repositories.py  ←── session_scope (db/ / SQLite)
    │  state machines and persistence helpers
    ▼
Celery task (jobs/celery_app.py; jobs/celery_worker.py is a shim)
    │  dispatched via TASK_MAP; broker + result backend = SQLite
    ▼
orchestration/runners/boot_capture_runner.py / live_session / playground/orchestrator.py
    │  long-running job and Playground logic — VM boot, wait, capture, teardown
    ▼
libvirt / virsh / SSH / virtiofsd
    ▲
    └── core/fake_mode.py gates the ASTINUSLAB_FAKE_* test doubles that stand in for
        libvirt in the test tiers (never set in a real deployment)
```

---

## Module map

Paths are relative to `src/astinuslab/`. Packages group modules that were split
out of the original flat layout: `api/`, `core/`, `db/`, `jobs/`,
`orchestration/`, `virtualization/`, `images/`, `storage/`, `workloads/`,
`forensics/symbols/`, and `playground/`. The original root module names for
split modules were removed by the legacy-code audit; use the canonical package
paths documented below.

### Core / entrypoints

| File | Responsibility |
|------|---------------|
| `main.py` | FastAPI app creation, exception handlers, security headers, static mount, `/health` |
| `api/__init__.py` | REST router (`/api/v1/*`), path validation for artifact downloads |
| `api/errors.py` | Structured API error envelope: `{"detail", "code", "context"}` |
| `forensics/api.py` | Versioned Volatility JSON API endpoints |
| `serve.py` | API server entrypoint wrapper — prints friendly config errors, then starts uvicorn |
| `orchestration/boot.py` | Celery entry-point shim; lazy imports defer the runner import until a task executes |
| `dbcli.py` | Database CLI entrypoint wrapper with friendly config-error reporting |
| `core/config.py` | `Settings` (Pydantic) — reads `astinuslab.toml` |
| `app_settings.py` | Database-backed application settings (retention windows) |
| `core/security.py` | Optional single-admin bearer/cookie auth: `Principal`, `require_admin_request`, token helpers |
| `core/fake_mode.py` | Central read of the `ASTINUSLAB_FAKE_LIBVIRT`/`ASTINUSLAB_FAKE_PLAYGROUND` test-double flags; banner + `/health`/manifest surface |
| `core/logging_config.py` | Structured logging setup |
| `i18n.py` | Minimal internationalisation helpers (`i18n_data/` holds the dictionaries) |
| `core/system_status.py` | Read-only probes (services, libvirt network/leases, fake mode) for the Settings page |
| `core/locks.py` | Capture locks for single-host runner flows (in-process + `flock`) |

### Data model & persistence

| File | Responsibility |
|------|---------------|
| `db/models.py` | SQLAlchemy ORM for cases, acquisitions, artifacts, reports, jobs, host actions, runtimes, ISF artifacts, Playground sessions/resources, topology layout, and app settings |
| `api/schemas.py` | Pydantic I/O schemas + `JobStatus` literal type |
| `jobs/job_status.py` | `ALLOWED_TRANSITIONS` job state machine definition |
| `db/repositories.py` | `JobRepository` — CRUD + enforced state machine |
| `db/__init__.py` | Engine, `session_scope`, Alembic-backed schema initialization |
| `db/baseline_snapshot.py` | Frozen schema snapshot at the Alembic baseline revision |
| `catalog.py` | YAML catalog loaders: targets, scenarios, workloads, attackers |
| `core/compatibility.py` | Normalise workload/target compatibility selectors and evaluate inclusion/exclusion |

### Services & job execution

| File | Responsibility |
|------|---------------|
| `jobs/job_services.py` | Job lifecycle service: creation, cancellation, runtime control, retention; `safe_artifact_path` |
| `forensics/services.py` | Capture and Volatility 3 service operations |
| `forensics/case_services.py` | Case, acquisition, artifact, report, and CVE service operations |
| `jobs/job_control.py` | Cancel/capture request signalling and subprocess kill |
| `jobs/runtime_control.py` | `control_runtime` / `refresh_runtime` — reboot/shutdown a running role |
| `jobs/runtime_actions.py` | Runtime action helpers backing `control_runtime` |
| `jobs/runtime_support.py` | `JobRuntime` record management, power state reads |
| `jobs/job_state_support.py` | Load job fields, preserve-failed-VM flag |
| `orchestration/deps.py` | `JobRunDeps` dependency container — wiring + test injection seam for job runs |
| `orchestration/context.py` | `JobRunContext` — per-run shared state |
| `orchestration/lab_lifecycle.py` | Shared lab lifecycle operations: teardown, lab snapshots, cleanup |
| `orchestration/support.py` | Lab snapshot create/revert, action state transitions |
| `core/output_parsers.py` | Parse `KEY=VALUE` output from guest scripts |
| `jobs/startup_recovery.py` | Reconcile orphaned jobs on worker start |
| `orchestration/runners/boot_capture_runner.py` | Job execution logic for boot-capture and post-script modes |
| `orchestration/runners/isf_matrix_runner.py` | CLI/library entrypoint to run ISF extraction across the target catalog in parallel |
| `live_session/runner.py` | Live (lab) session job flow |
| `live_session/support.py` | Attacker boot parallelisation, capture handoff |

### Celery

| File | Responsibility |
|------|---------------|
| `jobs/celery_app.py` | Celery app, task definitions, and action task map |
| `jobs/celery_main.py` | Celery worker entrypoint that registers worker-only startup hooks |
| `jobs/celery_worker.py` | Backward-compatible Celery import shim |
| `jobs/celery_worker_signals.py` | Worker startup orphan recovery signal handler |

### Virtualization (`virtualization/`)

| File | Responsibility |
|------|---------------|
| `virtualization/vm_lifecycle.py` | `VMLifecycle` — libvirt boot with cloud-init + virtiofs |
| `virtualization/vm_support.py` | VM teardown, disk paths, target/scenario look-ups |
| `virtualization/guest_wait.py` | Polls for SSH readiness, QEMU Guest Agent, capture trigger |
| `virtualization/guest_script_support.py` | Guest-side script delivery via virtiofs |
| `virtualization/process_support.py` | `run_command` — timeout-aware subprocess with cancel check |
| `virtualization/host_helpers.py` | Resolve privileged helper script paths under `/usr/local/libexec/astinuslab` |
| `virtualization/boot_staging.py` | Backing-image install, guest metadata, target cloud-init staging |
| `virtualization/cloud_init.py` | Render target user-data / meta-data / network-config for cloud-init |
| `virtualization/entrypoint_support.py` | `format_config_error` / `die_config` for CLI entrypoints |
| `virtualization/host_script_manifest.py` | Declarative contract for host shell scripts invoked by the backend |

### Images (`images/`)

| File | Responsibility |
|------|---------------|
| `images/image_inspection.py` | `virt-inspector`-based image metadata extraction with sidecar JSON cache |
| `images/image_admin_support.py` | Image download + integrity helpers |

### Storage (`storage/`)

| File | Responsibility |
|------|---------------|
| `storage/artifacts.py` | Artifact manifest + `.sha256` sidecar authoring (stamps `integrity.fake_mode`) |
| `storage/keystore.py` | SSH key pair management |
| `storage/retention.py` | Artifact retention policy |
| `storage/run_path_support.py` | Resolve image path, post-boot script execution |
| `storage/storage_admin_support.py` | Storage directory helpers |

### Workloads (`workloads/`)

| File | Responsibility |
|------|---------------|
| `workloads/workload_support.py` | Workload install, health checks, network isolation |
| `workloads/workload_cache_support.py` | QCOW2 workload snapshot cache index |

### Forensics (`forensics/`)

| File | Responsibility |
|------|---------------|
| `forensics/capture_support.py` | `acquire_and_publish` — memory/disk capture + artifact promotion |
| `forensics/memory_dump.py` | `MemoryDumper` — libvirt `virDomainCoreDump` wrapper |
| `forensics/manual_upload.py` | Operator-supplied (manual) ISF and memory-dump uploads |
| `forensics/isf_preflight.py` | Host-side preflight check for required ISF extraction tools (`rpm2cpio`, `virt-customize`, …) |
| `forensics/isf_diagnostics.py` | `write_diagnostic_bundle` — serial logs, traceback, and dumpxml on terminal ISF failures |
| `forensics/vol3_support.py` | Volatility 3 Linux ISF provider chain and symbol-build workflow |
| `forensics/vol3_analysis.py` | Job-local Volatility 3 analysis against captured memory |
| `forensics/vol3_plugin_run.py` | On-demand workbench plugin runs and run-file persistence |
| `forensics/vol3_plugins.py` | Volatility 3 plugin catalog |

#### Symbols (`forensics/symbols/`)

| File | Responsibility |
|------|---------------|
| `forensics/symbols/base.py` | Provider protocol + result dataclasses (`KernelMetadata`, `FetchResult`, `SymbolProvider`) |
| `forensics/symbols/build_id.py` | Extract kernel build-id from the guest |
| `forensics/symbols/cache.py` | `SymbolCache` — on-disk ISF cache |
| `forensics/symbols/identity.py` | `SymbolIdentity` → ISF filename derivation |
| `forensics/symbols/providers.py` | Prebuilt-mirror and debuginfod ISF providers |
| `forensics/symbols/runner.py` | Provider diagnostic summary (`describe_fetch`); does not execute symbol fetching |
| `forensics/symbols/windows.py` | Windows PDB symbol sourcing for Volatility 3 |

### Playground subsystem

| File | Responsibility |
|------|---------------|
| `playground/api.py` | Router assembly and shared route helpers for the `/api/v1/playground` surface |
| `playground/routes/` | Route modules for sessions, VMs, VM actions, NICs, networks, captures, metrics, topology, catalog, and recipes |
| `playground/orchestrator.py` | Playground session lifecycle — VM boot/destroy, network create/destroy, capture orchestration |
| `playground/repositories.py` | Persistence helpers for `Job`/`JobVM`/`JobNetwork`/`JobVMNic`/`PlaygroundTopologyLayout` rows |
| `playground/workloads.py` | Playground workload validation, compatibility, install, and health checks |
| `playground/networks.py` | Libvirt network XML templates and IP-range helpers |
| `playground/captures.py` | `DiskCapture` — pause-convert and blockcopy disk capture for Playground VMs |
| `playground/metrics.py` | Libvirt domain CPU%/MEM% sampler with process-local delta cache |
| `playground/cleanup.py` | `purge_session_resources` — destroy all session VMs/networks and transition to terminal state |
| `playground/reconciliation.py` | Reconcile orphaned Playground VMs/networks against libvirt on worker start |
| `playground/recipes.py` | JSON Playground recipe import/export validation |
| `playground/admission.py` | VM admission control against `orchestration.max_concurrent_vms` |
| `playground/enqueue.py` | Enqueue Playground tasks or raise if the queue is busy |
| `playground/serializers.py` | Playground API response serializers |
| `playground/constants.py` | Shared Playground network default constants |

### UI (`ui/`)

`ui/` holds the server-rendered HTML route modules (jobs, images, storage,
settings, SSH keys, logs, artifacts, auth, and the Volatility workbench) plus
their template and context helpers.

---

## Job state machine

Defined in `job_status.py:ALLOWED_TRANSITIONS` and enforced by `JobRepository.transition_job_status()`. Illegal transitions raise `InvalidJobTransitionError` (HTTP 409).

Edges enumerated below, grouped by source state. Each bullet lists every
allowed successor for that state. Terminal states (`completed`, `failed`,
`cancelled`) have no outgoing edges.

- `queued` → `preparing_image`, `waiting_for_guest_access`, `cancel_requested`, `cancelled`, `failed`
- `preparing_image` → `waiting_for_guest_access`, `cancel_requested`, `cancelled`, `failed`
- `waiting_for_guest_access` → `mounting_output_share`, `cancel_requested`, `cancelled`, `failed`
- `mounting_output_share` → `running_scenario_script`, `provisioning_workload`, `waiting_for_capture`, `acquiring_memory`, `acquiring_disk`, `generating_isf`, `cancel_requested`, `cancelled`, `failed`
- `generating_isf` → `completed`, `cancel_requested`, `cancelled`, `failed`
- `running_scenario_script` → `waiting_for_capture`, `acquiring_memory`, `acquiring_disk`, `cancel_requested`, `cancelled`, `failed`
- `provisioning_workload` → `waiting_for_capture`, `cancel_requested`, `cancelled`, `failed`
- `waiting_for_capture` → `capture_requested`, `acquiring_memory`, `acquiring_disk`, `cancel_requested`, `cancelled`, `failed`
- `capture_requested` → `acquiring_memory`, `acquiring_disk`, `cancelled`, `failed`
- `acquiring_memory` → `publishing_artifact`, `acquiring_disk`, `cancelled`, `failed`
- `acquiring_disk` → `publishing_artifact`, `cancelled`, `failed`
- `publishing_artifact` → `completed`, `cancelled`, `failed`
- `cancel_requested` → `cancelled`, `failed`
- `completed` → (terminal)
- `failed` → (terminal)
- `cancelled` → (terminal)

---

## Execution modes

| Mode | Celery task | What it does |
|------|------------|-------------|
| `immediate` | `run_immediate_job` | Boot → capture → teardown |
| `post_script` | `run_post_script_job` | Boot → run scenario script → capture → teardown |
| `manual_trigger` | `run_manual_trigger_job` | Boot → optional workload install → snapshot → wait for operator → capture → teardown |

---

## Task queue

Celery broker and result backend both use SQLite files under `storage/`.
`TASK_MAP` in `celery_app.py` maps `action_type` strings to task
functions so `_dispatch_pending_actions` can chain DB-driven actions.

On worker start, `celery_worker_signals.py` registers `@worker_ready` to reconcile
stale in-progress jobs, fail interrupted host actions, and tear down any
target and attacker VMs left behind by the previous worker.

---

## Database

SQLite at `storage/astinuslab.db`. Schema is managed by `db/__init__.py` with package-owned Alembic revisions in `migrations/` for the single-host deployment model:
- new installs: `Base.metadata.create_all` + stamp of the Alembic baseline
- pre-Alembic databases: a frozen legacy bootstrap ports them to the baseline schema, then stamps
- stamped databases: `alembic upgrade head`; new schema changes are Alembic revisions
- `python -m astinuslab.dbcli init` runs the create/upgrade path used by install and local startup (wraps `astinuslab.db` with friendly config-error reporting; `python -m astinuslab.db init` still works)

Tables: `cases`, `acquisitions`, `acquisition_events`, `artifacts`, `reports`, `jobs`, `playground_sessions`, `job_events`, `host_actions`, `job_runtimes`, `isf_artifacts`, `job_vms`, `job_networks`, `playground_topology_layout`, `job_vm_nics`, `app_settings`

---

## Configuration

`core/config.py` loads `astinuslab.toml` (if present) over pydantic defaults;
the canonical `core/config.py` path is the only supported configuration module.
All paths support `{base_dir}` substitution and relative-to-repo resolution.
Key settings:

```toml
[orchestration]
libvirt_uri = "qemu:///system"
libvirt_network = "default"

[storage]
storage_dir = "storage"
images_dir = "images/definitions"

[vol3]
enabled = true

[image_inspection]
enabled = true
virt_inspector_bin = "virt-inspector"
```

---

## Artifact path security

`jobs/job_services.py:safe_artifact_path` (imported into `api/routes.py` as
`_safe_artifact_path`) resolves the requested path and verifies it is inside
`storage/dumps/` before serving. Auth check runs before the existence check to
avoid a filesystem oracle via 404 vs 403.

---

## Fake mode

Three independent flags cover different test boundaries. They are
**never** set by `install.sh`, any systemd unit, or `astinuslab.toml`.
`core/fake_mode.py` reads them once at import and is the single source of truth.

| Env var | Effect |
|---------|--------|
| `ASTINUSLAB_FAKE_LIBVIRT` | `__init__.py` force-replaces `sys.modules["libvirt"]` with an in-process fake at package import. The fake domain has no `coreDump`, so the forensics memory-capture path fails loudly rather than forging evidence. |
| `ASTINUSLAB_FAKE_PLAYGROUND` | `playground/orchestrator.py` writes placeholder bytes for memory/disk captures instead of calling libvirt/`qemu-img`. |
| `ASTINUSLAB_FAKE_ORCHESTRATION` | Replaces Job host dependencies through `JobRunDeps`; manual Jobs can reach `waiting_for_capture`, while real acquisition and unanticipated host operations fail closed. |

The first two flags do not isolate Job execution. Before driving a Job beyond
admission, set `ASTINUSLAB_FAKE_ORCHESTRATION=1` and use a temporary database,
broker/result state and storage roots separate from operator data. Follow
[Testing](../../docs/development/testing.md#driving-a-job-past-admission) for the
supported isolated setup. A simulated boot or placeholder capture is not real
acquisition evidence.

Because a faked artifact is byte-for-byte reproducible it still hashes cleanly, so
a checksum proves nothing about it. To keep the forgery legible:

- `build_artifact_manifest` stamps `integrity.fake_mode` (bool) and
  `integrity.fake_mode_flags` into every manifest. Absence means "unknown"
  (manifest written before 2026-07-09), never "false".
- Each fake capture logs a `FAKE MODE` warning to the job log.
- A banner is printed to stderr at import, returned by `/health` (`fake_mode` +
  `warning` keys, present only when faking), and shown on the Settings page.

---

## Volatility 3 integration

`forensics/vol3_support.py` owns the Linux ISF workflow. For `immediate`, setting `build_vol3_isf=true` means `ISF-only`: the job boots the
guest and generates the Volatility 3 symbol file without acquiring memory or
disk artifacts. `manual_trigger` jobs keep acquisition and symbol generation
separate; `build_vol3_isf` is rejected for that mode.

---

## Tests

```bash
.venv/bin/python -m pytest tests/python -q
```

Tests are organised into domain folders under `tests/python/`: `api/`, `forensics/`, `jobs/`, `playground/`, `host/`, `ui/` (server-rendered route tests), and `integration/`. Cross-cutting tests remain in the root. `conftest.py` provides shared fixtures. See CI output for the current test count.
