# Components

## Backend Modules

| Path | Responsibility |
| --- | --- |
| `src/astinuslab/main.py` | FastAPI app construction, static mount, router registration, health check, admin-auth gate, and security-header (CSP) middleware. |
| `src/astinuslab/core/` | Cross-cutting platform policy: configuration, compatibility, fake-mode policy, locks, logging, request context, security, readiness probes, output parsing, and host status. The canonical package paths are the only supported locations. |
| `src/astinuslab/api/routes.py` | Main `/api/v1` REST routes for catalogs, jobs, acquisitions, artifacts, runtimes, and retention. |
| `src/astinuslab/playground/api.py` | `/api/v1/playground` router assembly and shared route helpers. |
| `src/astinuslab/playground/routes/` | Playground route modules for sessions, networks, VMs, NICs, captures, metrics, topology, catalog, and recipes. |
| `src/astinuslab/api/schemas.py` | Pydantic request and response models. |
| `src/astinuslab/core/config.py` | `astinuslab.toml` loading, environment overrides, path resolution, and settings validation. |
| `src/astinuslab/db/` | SQLite database initialization, ORM models, repositories, and migrations. |
| `src/astinuslab/jobs/job_services.py` | Job lifecycle, runtime control, and retention service operations. |
| `src/astinuslab/forensics/services.py` | Capture and Volatility 3 service operations. |
| `src/astinuslab/forensics/case_services.py` | Case, artifact, report, acquisition, and CVE service operations. |
| `src/astinuslab/jobs/celery_app.py` | Celery app, task definitions, and `TASK_MAP` dispatch table. |
| `src/astinuslab/jobs/celery_main.py` | Celery app import target used by launcher and systemd worker commands. |
| `src/astinuslab/jobs/celery_worker.py` | Backward-compatible import shim for worker entrypoints. |
| `src/astinuslab/orchestration/runners/boot_capture_runner.py` | Scenario-driven VM boot, acquisition, and post-script job flows. |
| `src/astinuslab/orchestration/deps.py` | Explicit dependency container (`JobRunDeps`) wiring the job-run flows; the test injection seam. |
| `src/astinuslab/virtualization/` | Libvirt domain/network/disk lifecycle, cloud-init seed build, boot staging, QGA/SSH guest waits, and host/guest script support. |
| `src/astinuslab/playground/orchestrator.py` | Playground session, network, VM, capture, and cleanup tasks. |
| `src/astinuslab/playground/repositories.py` | Playground persistence helpers. |
| `src/astinuslab/playground/recipes.py` | JSON Playground recipe import/export validation. |
| `src/astinuslab/catalog.py` | Catalog loading, image discovery, and image metadata inference. |
| `src/astinuslab/forensics/vol3_support.py` | Volatility 3 Linux ISF provider chain and symbol-build workflow. |
| `src/astinuslab/forensics/symbols/` | ISF identity, cache, providers, and runner logic. |
| `src/astinuslab/forensics/api.py` | `/api/v1/volatility` workbench REST routes: dumps, plugin catalog, on-demand plugin runs, queue, and per-run results. |
| `src/astinuslab/live_session/` | Live-session (Interactive Lab, `manual_trigger` wire name) run flow: `runner.py` owns the run flow, `support.py` owns attacker boot, workload provisioning, and capture handoff. |
| `src/astinuslab/orchestration/runners/isf_matrix_runner.py` | Batch ISF matrix runs across target images. |
| `src/astinuslab/storage/` | Artifact manifests and SHA-256 sidecars, retention sweeps, SSH keystore, run paths, and Storage admin support. |
| `src/astinuslab/images/` | Image upload/admin operations and image metadata inspection. |
| `src/astinuslab/workloads/` | Workload provisioning helpers and the workload package cache. |
| `src/astinuslab/ui/` | HTML UI route modules. |

## Frontend

| Path | Responsibility |
| --- | --- |
| `src/astinuslab/web/templates/` | Jinja2 templates for dashboard, jobs, admin pages, playground, and partials. |
| `src/astinuslab/web/static/` | CSS and JavaScript assets. |
| `src/astinuslab/web/static/playground/` | Playground client modules for state, API calls, polling, map rendering, modals, and wizard behavior. |

## Host Scripts

| Path | Responsibility |
| --- | --- |
| `astinuslab.sh` | Development launcher, process control, cleanup, docs serving, and image scanning. |
| `ops/deploy/install.sh` | Release installer for `/opt/astinuslab`, venv, storage, helpers, sudoers, AppArmor, and systemd. |
| `ops/deploy/uninstall.sh` | Release uninstall workflow. |
| `ops/host/` | Host-side libvirt, disk, network isolation, workload cache, and Volatility helper scripts. |
| `ops/images/` | Operator-facing image download and provenance metadata helpers. |
| `ops/systemd/` | Source templates for installed API, runner, and analysis services. |
| `tools/dev/` | Development helper scripts for venv, DB, cleanup, and ISF matrix runs. |
| `tools/quality/` | Repository quality-policy checks run by CI. |
| `tools/release/` | Release evidence, validation, and review contracts. |
| `guest/scripts/` | Scripts copied into guest VMs for environment detection, disk checks, package helpers, and symbol builds. |

## Configuration and Data

| Path | Responsibility |
| --- | --- |
| `astinuslab.toml` | Main backend settings. |
| `config/astinuslab.env.example` | Example launcher/systemd environment. |
| `config/catalog/` | Targets, scenarios, workloads, attacker images, network nodes, and profiles. |
| `images/definitions/` | Local cloud images and image metadata sidecars. |
| `storage/` | Runtime database, logs, artifacts, temporary files, SSH keys, and workload cache. |
| `vol3/` | Volatility 3 symbol scripts and reusable symbol cache; operator tools remain outside the repository. |

## Scaling

### Scaling ceiling (V1 by design)

AstinusLab V1 is a single-host, single-runner system. The load-bearing invariant is: SQLite Celery broker, exactly one **capture/lifecycle** worker plus one dedicated `analysis`-queue worker (each `--concurrency=1`), and one host owning libvirt/storage operations. The analysis worker runs only `run_vol3_plugin` (read-only, no VM or guest contact); same-job Volatility 3 work is serialized across both workers by a per-job file lock, and both workers keep DB write transactions short under SQLite WAL.

Naively adding a second runner or raising worker concurrency can break three assumptions:

- The SQLAlchemy/SQLite Celery broker is polling-based and not the scaling boundary for concurrent workers.
- Capture and runtime guards combine `flock` with in-process locks; those do not provide distributed lock semantics across hosts or arbitrary worker processes.
- Reconciliation, waiting-capture expiry, retention, and long acquisition tasks share the same worker thread, so a long capture can delay cleanup by design. Interactive workbench plugin runs are exempt: they execute on the analysis worker.

Product-level VM capacity is a separate, safe knob: `[orchestration] max_concurrent_vms` admission-controls how many VMs may be active at once (Playground VMs, or a Job target+attacker pair; never both, because Playground and Jobs remain mutually exclusive). It is enforced with a count-then-insert under SQLite `BEGIN IMMEDIATE` and released by the same reconciliation paths that terminalize VM rows. Raising it never requires, and must never be implemented by, raising worker `--concurrency`.

Worker-level scaling is a different project: move the broker to Redis or another production broker first, redesign runner locks/reconciliation for multiple workers, then revisit database/storage scaling if load requires it. Until then, keep one runner at concurrency 1.
